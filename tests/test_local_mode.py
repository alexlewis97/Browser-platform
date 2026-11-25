"""Local-mode serving, auth, CORS, Chrome discovery, and frontend default URL."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from browser_platform.app import create_app, web_site_dir
from browser_platform.chrome_launch import (
    cdp_is_listening,
    default_profile_dir,
    find_chrome,
    launch_chrome_cdp,
)
from browser_platform.config import Settings
from browser_platform.local_access import is_loopback_host

ROOT = Path(__file__).resolve().parents[1]


def _settings(data_dir: Path, **kwargs) -> Settings:
    values = dict(
        data_dir=data_dir,
        pomerado_host=data_dir / "no-such-host.mjs",
        auto_launch_chrome=False,
        open_ui=False,
        host="127.0.0.1",
    )
    values.update(kwargs)
    return Settings(**values)


def test_serves_built_web_app_at_root(data_dir: Path):
    site = web_site_dir(ROOT)
    assert site is not None, "web/site must be built (index.html) for local serving"
    client = TestClient(create_app(_settings(data_dir), root=ROOT))
    home = client.get("/")
    assert home.status_code == 200
    assert "text/html" in home.headers.get("content-type", "")
    body = home.text.lower()
    assert "<!doctype html>" in body or "<html" in body
    # API routes are unchanged
    assert client.get("/health").json()["ok"] is True
    listed = client.get("/api/workflows")
    assert listed.status_code == 200
    # Bundled assets if present
    assets = list((site / "assets").glob("*")) if (site / "assets").is_dir() else []
    if assets:
        rel = assets[0].name
        resp = client.get(f"/assets/{rel}")
        assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_localhost_skips_token_non_local_still_requires(data_dir: Path):
    settings = _settings(data_dir, access_token="test-token-not-for-production")
    app = create_app(settings, root=ROOT)
    remote = TestClient(app)
    assert remote.get("/health").status_code == 200
    assert remote.get("/").status_code == 200  # static UI is open
    denied = remote.get("/api/workflows")
    assert denied.status_code == 401
    allowed = remote.get("/api/workflows", headers={"X-Browser-Platform-Token": "test-token-not-for-production"})
    assert allowed.status_code == 200

    transport = ASGITransport(app=app, client=("127.0.0.1", 50123))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8745") as local:
        skipped = await local.get("/api/workflows")
        assert skipped.status_code == 200, skipped.text


@pytest.mark.asyncio
async def test_require_token_on_localhost_enforced(data_dir: Path):
    settings = _settings(
        data_dir,
        access_token="test-token-not-for-production",
        require_token_on_localhost=True,
    )
    app = create_app(settings, root=ROOT)
    transport = ASGITransport(app=app, client=("127.0.0.1", 50124))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8745") as local:
        denied = await local.get("/api/workflows")
        assert denied.status_code == 401
        ok = await local.get("/api/workflows", headers={"X-Browser-Platform-Token": "test-token-not-for-production"})
        assert ok.status_code == 200


def test_cors_allows_localhost_ports_and_vercel(data_dir: Path):
    settings = _settings(data_dir, cors_origins="https://browser-platform-web.vercel.app")
    client = TestClient(create_app(settings, root=ROOT))

    def origin_allowed(origin: str) -> str | None:
        response = client.get("/health", headers={"Origin": origin})
        return response.headers.get("access-control-allow-origin")

    assert origin_allowed("http://localhost:8745") == "http://localhost:8745"
    assert origin_allowed("http://localhost:5173") == "http://localhost:5173"
    assert origin_allowed("http://127.0.0.1:3000") == "http://127.0.0.1:3000"
    assert origin_allowed("https://browser-platform-web.vercel.app") == "https://browser-platform-web.vercel.app"
    preflight = client.options(
        "/api/workflows",
        headers={
            "Origin": "http://localhost:8745",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.headers.get("access-control-allow-origin") == "http://localhost:8745"
    assert origin_allowed("https://evil.example") != "https://evil.example"


def test_frontend_defaults_backend_url_to_local_origin():
    source = (ROOT / "web" / "src" / "storage.ts").read_text()
    assert "window.location.origin" in source
    assert "export function defaultBackendUrl" in source
    assert "export function isLocalOrigin" in source
    site = ROOT / "web" / "site"
    bundle = " ".join(p.read_text(errors="ignore") for p in site.glob("assets/*.js")) if site.exists() else ""
    if bundle:
        assert "location.origin" in bundle


def test_loopback_and_chrome_discovery(tmp_path: Path, monkeypatch):
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("example.com")
    chrome = find_chrome()
    # On this Linux box Chrome or Playwright Chromium may be present; discovery must not crash.
    assert chrome is None or chrome.is_file()

    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        assert cdp_is_listening(url)
        assert launch_chrome_cdp(cdp_url=url, binary=Path("/usr/bin/false"), profile_dir=tmp_path) is None

    # A definitely-closed high port should report as down (bind then close).
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    assert not cdp_is_listening(f"http://127.0.0.1:{closed_port}")

    # Don't flip os.name: Path() would become WindowsPath and fail on Linux.
    from browser_platform.chrome_launch import _windows_chrome_candidates

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "Program Files"))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "Program Files (x86)"))
    candidates = _windows_chrome_candidates()
    assert any(path.name == "chrome.exe" for path in candidates)
    assert any("Google" in path.parts and "Chrome" in path.parts for path in candidates)
    linux_profile = default_profile_dir()
    assert linux_profile.name == "chrome-profile"
    assert "BrowserPlatform" in linux_profile.parts
