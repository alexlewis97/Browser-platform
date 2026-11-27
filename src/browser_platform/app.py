"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
import shutil
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from browser_platform.api.record import router as record_router
from browser_platform.api.rest import router as rest_router
from browser_platform.api.chat import router as chat_router
from browser_platform.api.ws import router as ws_router
from browser_platform.cache.action_cache import ActionCache
from browser_platform.fuzzy.hitl import ConfirmationBroker, EventHub
from browser_platform.cache.result_cache import ResultCache
from browser_platform.chrome_launch import maybe_launch_cdp
from browser_platform.config import Settings, load_settings
from browser_platform.local_access import CORS_ORIGIN_REGEX, access_token_ok, validate_public_bind
from browser_platform.orchestrator.runner import WorkflowRunner
from browser_platform.pomerado.adapter import PomeradoAdapter
from browser_platform.pomerado.chromium import ensure_pomerado_chromium, profile_dir_for
from browser_platform.pomerado.keys import looks_like_live_key
from browser_platform.local_secrets import apply_stored_openai_key, openai_configured
from browser_platform.pomerado.trace import backfill_known_jobs
from browser_platform.pomerado.usage_proxy import spend_cap_from_env, start_usage_proxy
from browser_platform.redaction import install_logging_redaction
from browser_platform.store.file_store import FileStore

logger = logging.getLogger("browser_platform")

ALWAYS_OPEN_PATHS = frozenset({"/health"})
DOCS_PATHS = frozenset({"/docs", "/openapi.json", "/redoc"})


def _is_public_catalog_path(path: str) -> bool:
    """GET /api/catalog and GET /api/catalog/{id} are public (end-user run site)."""
    value = path.rstrip("/") or "/"
    if value == "/api/catalog":
        return True
    prefix = "/api/catalog/"
    if value.startswith(prefix):
        rest = value[len(prefix):]
        return bool(rest) and "/" not in rest
    return False


def _is_open_path(path: str, method: str, settings: Settings | None) -> bool:
    if path in ALWAYS_OPEN_PATHS:
        return True
    # Docs stay open only when no access token is configured (local-only mode).
    token = (getattr(settings, "access_token", "") or "") if settings is not None else ""
    if path in DOCS_PATHS and not token:
        return True
    if method in {"GET", "HEAD"} and _is_public_catalog_path(path):
        return True
    # Static UI (same-origin) never requires a token.
    if method in {"GET", "HEAD"} and not path.startswith("/api") and not path.startswith("/ws"):
        return True
    return False


class AccessTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings: Settings | None = getattr(request.app.state, "settings", None)
        if request.method == "OPTIONS" or _is_open_path(request.url.path, request.method, settings):
            return await call_next(request)
        # HTTP routes: header-only token (no query string — leaks in access logs).
        if access_token_ok(request, settings, allow_query=False):
            return await call_next(request)
        return JSONResponse({"detail": "invalid or missing access token"}, status_code=401)




def _pomerado_available(settings: Settings) -> bool:
    if not settings.pomerado_enabled:
        return False
    if not settings.pomerado_host.exists():
        return False
    if shutil.which("node") is None:
        return False
    return True


def web_site_dir(root: Path) -> Path | None:
    site = root / "web" / "site"
    if (site / "index.html").is_file():
        return site
    return None


def _mount_web_app(app: FastAPI, root: Path) -> None:
    """Serve the built web UI at / (same origin as the API)."""

    site = web_site_dir(root)
    if site is None:
        return
    assets = site / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="site-assets")

    index = site / "index.html"

    @app.get("/", include_in_schema=False)
    async def site_index() -> FileResponse:
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})

    # Other static files next to index.html (favicon, robots, etc.).
    @app.get("/{path:path}", include_in_schema=False)
    async def site_static(path: str):
        if path.startswith("api/") or path.startswith("ws/") or path in {"health", "docs", "openapi.json", "redoc"}:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = (site / path).resolve()
        try:
            candidate.relative_to(site.resolve())
        except ValueError:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})


def ensure_openai_runtime(app: FastAPI) -> None:
    """Load a machine-local OpenAI key and start the usage proxy if a live key is present."""
    apply_stored_openai_key()
    if not looks_like_live_key(os.environ.get("OPENAI_API_KEY") or ""):
        return
    if getattr(app.state, "openai_usage_proxy", None) is not None:
        return
    settings = getattr(app.state, "settings", None)
    try:
        from browser_platform.pomerado.usage_proxy import spend_cap_from_env, start_usage_proxy

        ledger = (settings.data_dir / "pomerado_openai_spend.json") if settings is not None else None
        proxy = start_usage_proxy(cap_usd=spend_cap_from_env(), ledger_path=ledger)
        app.state.openai_usage_proxy = proxy
        adapter = getattr(app.state, "pomerado", None)
        if adapter is not None:
            adapter.openai_base_url = proxy.base_url
            adapter.usage_tracker = proxy.tracker
    except Exception:
        logger.exception("OpenAI usage proxy failed to start")


def create_app(
    settings: Settings | None = None,
    *,
    root: Path | None = None,
    repair_client=None,
) -> FastAPI:
    install_logging_redaction()
    apply_stored_openai_key()
    root = root or Path(__file__).resolve().parents[2]
    settings = (settings or load_settings(root)).resolve(root)
    validate_public_bind(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    store = FileStore(settings.data_dir)
    event_hub = EventHub()
    jobs_dir = settings.data_dir / "pomerado_jobs"
    pomerado = None
    if _pomerado_available(settings):
        pomerado = PomeradoAdapter(
            settings.pomerado_host,
            root=settings.data_dir / "integrations",
            endpoint=None,
            model=settings.default_model,
            store=store,
            event_hub=event_hub,
            jobs_dir=jobs_dir,
        )
        try:
            backfill_known_jobs(pomerado.jobs, data_dir=settings.data_dir, workflow_store=store)
        except Exception:
            logger.exception("Pomerado mint-trace backfill failed")
    confirmation_broker = ConfirmationBroker(event_hub)
    runner = WorkflowRunner(
        store,
        settings,
        action_cache=ActionCache(),
        result_cache=ResultCache(ttl_seconds=settings.result_cache_ttl_seconds),
        repair_client=repair_client,
        pomerado=pomerado,
        event_hub=event_hub,
        confirmation_broker=confirmation_broker,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        launched = None
        try:
            launched = maybe_launch_cdp(settings)
            if launched is not None:
                app.state.chrome_proc = launched
                logger.info("Launched a dedicated Chrome profile for CDP on %s", settings.default_cdp_url)
            if settings.pomerado_enabled and settings.pomerado_auto_launch_chromium:
                try:
                    pom_browser = ensure_pomerado_chromium(
                        profile_dir=profile_dir_for(settings.data_dir, settings.pomerado_chromium_profile),
                        auto_launch=True,
                        host_root=settings.pomerado_host.parent.parent,
                    )
                    app.state.pomerado_chromium = pom_browser
                    adapter = getattr(app.state, "pomerado", None)
                    if adapter is not None:
                        adapter.endpoint = pom_browser.endpoint
                    logger.info("Pomerado Playwright server ready")
                except Exception:
                    logger.exception("Pomerado Chromium launch failed (mint/run will fail until it is up)")
            if looks_like_live_key(os.environ.get("OPENAI_API_KEY") or ""):
                try:
                    proxy = start_usage_proxy(
                        cap_usd=spend_cap_from_env(),
                        ledger_path=settings.data_dir / "pomerado_openai_spend.json",
                    )
                    app.state.openai_usage_proxy = proxy
                    adapter = getattr(app.state, "pomerado", None)
                    if adapter is not None:
                        adapter.openai_base_url = proxy.base_url
                        adapter.usage_tracker = proxy.tracker
                except Exception:
                    logger.exception("OpenAI usage proxy failed to start")
            if settings.open_ui:
                webbrowser.open(f"http://{settings.host}:{settings.port}/")
        except Exception:
            logger.exception("Chrome auto-launch or UI open failed (backend will still start)")
        try:
            from browser_platform.authoring.remote_record import manager_from
            manager_from(app).start_reaper()
        except Exception:
            logger.exception("Remote recording session manager failed to start")
        yield
        try:
            manager = getattr(app.state, "record_sessions", None)
            if manager is not None:
                await manager.close_all()
        except Exception:
            logger.exception("Remote recording session cleanup failed")
        adapter = getattr(app.state, "pomerado", None)
        if adapter is not None and hasattr(adapter, "close"):
            await adapter.close()
        proxy = getattr(app.state, "openai_usage_proxy", None)
        if proxy is not None:
            try:
                proxy.close()
            except Exception:
                pass
        # Leave launched Chrome and Pomerado Chromium running so profiles / logins stay intact.

    # Hide OpenAPI docs when an access token is configured (tunneled / shared hosts).
    docs_enabled = not bool((settings.access_token or "").strip())
    app = FastAPI(
        title="browser-platform",
        description="Deterministic-first browser subagent orchestrator.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    # Auth is registered first so CORS (added next) runs first and can answer OPTIONS.
    app.add_middleware(AccessTokenMiddleware)
    origins = [o for o in settings.cors_origin_list if o != "*"]
    allow_all = "*" in settings.cors_origin_list
    # Exact configured origins + localhost regex only (not every *.vercel.app / chrome-extension).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if allow_all else origins,
        allow_origin_regex=None if allow_all else CORS_ORIGIN_REGEX,
        allow_credentials=not allow_all,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(rest_router)
    app.include_router(chat_router)
    app.include_router(record_router)
    app.include_router(ws_router)
    _mount_web_app(app, root)
    app.state.settings = settings
    app.state.store = store
    from browser_platform.orchestrator.chat import ChatJobs, ChatOrchestrator
    app.state.chat_jobs = ChatJobs()
    app.state.chat = ChatOrchestrator(store, runner=runner, jobs=app.state.chat_jobs)
    app.state.runner = runner
    app.state.pomerado = pomerado
    app.state.event_hub = event_hub
    app.state.confirmation_broker = confirmation_broker
    from browser_platform.store.from_task_jobs import FromTaskJobStore
    app.state.from_task_jobs = FromTaskJobStore(settings.data_dir / "from_task_jobs")
    return app
