"""Runtime settings. API keys are never read from settings or env for persistence."""

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from browser_platform import DEFAULT_MODEL
from browser_platform.local_access import DEFAULT_CORS_ORIGINS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BROWSER_PLATFORM_", extra="ignore")

    data_dir: Path = Field(default=Path("data"))
    default_model: str = DEFAULT_MODEL
    default_cdp_url: str = "http://127.0.0.1:9222"
    pomerado_host: Path = Field(default=Path("node_host/src/stock-mcp.mjs"))
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    result_cache_ttl_seconds: int = 300
    llm_max_tokens: int = 800
    llm_max_steps: int = 5
    action_timeout_ms: int = 8_000
    host: str = "127.0.0.1"
    port: int = 8745
    # Shared access token for a publicly tunneled API. Empty = loopback-only.
    access_token: str = ""
    # When a token is set it is required everywhere by default (including localhost).
    # Set false only for trusted same-machine clients that cannot send the header.
    require_token_on_localhost: bool = True
    # Comma-separated browser origins (Vercel URL). Localhost / 127.0.0.1 any port are always allowed.
    cors_origins: str = ",".join(DEFAULT_CORS_ORIGINS)
    # Auto-start the user's Chrome with --remote-debugging-port if 9222 is free.
    auto_launch_chrome: bool = True
    chrome_profile_dir: Path | None = None
    # Open the web UI in the default browser after bind (used by start.bat).
    open_ui: bool = False
    # Pomerado minting is optional; disabled automatically when Node is missing.
    pomerado_enabled: bool = True
    # Dedicated Pomerado Chromium (not the user's Chrome on default_cdp_url).
    pomerado_browser_cdp_url: str = "http://127.0.0.1:9333"
    pomerado_chromium_profile: Path | None = None
    pomerado_auto_launch_chromium: bool = True
    # Fuzzy-match tier (scores are 0–100, rapidfuzz).
    fuzzy_threshold: float = 90.0
    fuzzy_margin: float = 8.0
    fuzzy_top_n: int = 5
    fuzzy_include_screenshot: bool = False
    # Human-in-the-loop when fuzzy match is not confident.
    hitl_mode: str = "always_on_uncertain"
    hitl_timeout_s: float = 300.0
    hitl_timeout_policy: str = "fail"
    # Strict selectors: a step that only works through a non-primary strategy (another
    # recorded selector, cache, heuristic, fuzzy/alias, HITL, LLM repair) fails by name
    # instead of being rescued. Single platform default (env BROWSER_PLATFORM_STRICT_SELECTORS);
    # workflows (`strict_selectors`) and runs (`strict`) can override it.
    strict_selectors: bool = False
    # Named timeouts. Time spent waiting for a human confirmation (HITL) is bounded by
    # hitl_timeout_s instead and is not counted here.
    step_timeout_s: float = 60.0
    run_timeout_s: float = 240.0

    def resolve(self, root: Path | None = None) -> "Settings":
        base = root or Path.cwd()
        data = self.data_dir
        # Default "./data" on Windows goes to a persistent per-user folder so zip updates stay safe.
        if not data.is_absolute():
            if os.name == "nt" and str(data) in {"data", "./data"} and not (base / "data").exists():
                local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
                data = Path(local) / "BrowserPlatform" / "data"
            else:
                data = base / data
        host = self.pomerado_host if self.pomerado_host.is_absolute() else base / self.pomerado_host
        copy = self.model_copy()
        copy.data_dir = data
        copy.pomerado_host = host
        if copy.chrome_profile_dir is not None and not Path(copy.chrome_profile_dir).is_absolute():
            copy.chrome_profile_dir = base / copy.chrome_profile_dir
        if copy.pomerado_chromium_profile is None:
            copy.pomerado_chromium_profile = copy.data_dir / "pomerado-chromium-profile"
        elif not Path(copy.pomerado_chromium_profile).is_absolute():
            copy.pomerado_chromium_profile = base / copy.pomerado_chromium_profile
        return copy

    @property
    def cors_origin_list(self) -> list[str]:
        configured = [part.strip() for part in self.cors_origins.split(",") if part.strip()]
        if "*" in configured:
            return configured
        # Official frontends stay allowed even when an older env list omits a new site.
        seen: list[str] = []
        for origin in [*DEFAULT_CORS_ORIGINS, *configured]:
            if origin not in seen:
                seen.append(origin)
        return seen


def load_settings(root: Path | None = None) -> Settings:
    root = root or Path.cwd()
    env_file = root / ".env"
    if env_file.exists():
        return Settings(_env_file=env_file, _env_file_encoding="utf-8").resolve(root)
    return Settings().resolve(root)
