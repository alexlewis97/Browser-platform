"""Machine-local secrets (OpenAI key for Pomerado minting). Never log values."""

from __future__ import annotations

import os
from pathlib import Path

from browser_platform.pomerado.keys import looks_like_live_key

_FILENAME = "openai_api_key"


def config_dir() -> Path:
    override = (os.environ.get("BROWSER_PLATFORM_SECRETS_DIR") or "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "BrowserPlatform"
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "browser-platform"


def secrets_dir() -> Path:
    return config_dir() / "secrets"


def openai_key_path() -> Path:
    return secrets_dir() / _FILENAME


def _read_stored_key() -> str:
    path = openai_key_path()
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def stored_openai_key() -> str:
    """Return the stored key, or '' if none. Callers must not log the value."""
    return _read_stored_key()


def openai_configured() -> bool:
    env = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if looks_like_live_key(env):
        return True
    return looks_like_live_key(_read_stored_key())


def openai_key_source() -> str | None:
    env = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if looks_like_live_key(env):
        # Distinguish env-provided vs applied-from-store for the status API.
        if looks_like_live_key(_read_stored_key()) and env == _read_stored_key():
            return "local"
        return "env"
    if looks_like_live_key(_read_stored_key()):
        return "local"
    return None


def apply_stored_openai_key() -> bool:
    """If the process has no live key, load one from the local secrets file into the environment."""
    env = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if looks_like_live_key(env):
        return True
    stored = _read_stored_key()
    if looks_like_live_key(stored):
        os.environ["OPENAI_API_KEY"] = stored
        return True
    return False


def save_openai_key(key: str | None) -> bool:
    """Save or clear the local OpenAI key. Returns whether a live key is now configured."""
    value = (key or "").strip()
    path = openai_key_path()
    if not value:
        if path.is_file():
            try:
                path.unlink()
            except Exception:
                pass
        # Only drop the process env key if it came from our store (don't clobber a shell-provided key unless clearing).
        os.environ.pop("OPENAI_API_KEY", None)
        return False
    if not looks_like_live_key(value):
        raise ValueError("that does not look like a live OpenAI API key")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    os.environ["OPENAI_API_KEY"] = value
    return True


def key_status() -> dict[str, object]:
    return {
        "openai": {
            "configured": openai_configured(),
            "source": openai_key_source(),
        },
        "openrouter": {
            "stored_on_server": False,
            "used_for": ["browser-use", "repair", "from-task", "hitl"],
        },
    }


__all__ = [
    "apply_stored_openai_key",
    "config_dir",
    "key_status",
    "openai_configured",
    "openai_key_path",
    "openai_key_source",
    "save_openai_key",
    "secrets_dir",
    "stored_openai_key",
]
