"""Machine-local OpenAI key store and Settings API (never echo the key)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.config import Settings
from browser_platform.local_secrets import (
    apply_stored_openai_key,
    key_status,
    openai_configured,
    openai_key_path,
    save_openai_key,
)


ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "node_host" / "src" / "stock-mcp.mjs"


def _app(data_dir: Path):
    return create_app(Settings(data_dir=data_dir, pomerado_host=HOST, auto_launch_chrome=False, open_ui=False), root=ROOT)


def test_save_and_apply_openai_key_without_logging(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("BROWSER_PLATFORM_SECRETS_DIR", str(tmp_path / "secrets-root"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert openai_configured() is False
    key = "sk-" + ("qwertyuiop" * 4)
    # "live" is not in the blocklist; this is long enough.
    save_openai_key(key)
    assert openai_key_path().is_file()
    text = openai_key_path().read_text()
    assert key in text
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert apply_stored_openai_key() is True
    assert openai_configured() is True
    status = key_status()
    assert status["openai"]["configured"] is True
    blob = str(status)
    assert key not in blob
    save_openai_key("")
    assert not openai_key_path().is_file()


def test_rejects_placeholder_key(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("BROWSER_PLATFORM_SECRETS_DIR", str(tmp_path / "secrets-root"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        save_openai_key("sk-or-v1-not-used-in-unit-test-fake")
        raise AssertionError("placeholder key should be rejected")
    except ValueError:
        pass
    assert openai_configured() is False


def test_settings_keys_api_never_returns_secret(data_dir: Path, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("BROWSER_PLATFORM_SECRETS_DIR", str(tmp_path / "api-secrets"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    key = "sk-" + ("asdfghjklp" * 4)
    with TestClient(_app(data_dir)) as client:
        empty = client.get("/api/settings/keys")
        assert empty.status_code == 200, empty.text
        assert empty.json()["openai"]["configured"] is False
        assert key not in empty.text
        denied = client.put("/api/settings/openai", json={"api_key": "short"})
        assert denied.status_code == 400
        saved = client.put("/api/settings/openai", json={"api_key": key})
        assert saved.status_code == 200, saved.text
        assert saved.json()["openai"]["configured"] is True
        assert key not in saved.text
        got = client.get("/api/settings/keys")
        assert got.json()["openai"]["configured"] is True
        assert key not in got.text
        cleared = client.put("/api/settings/openai", json={"api_key": ""})
        assert cleared.status_code == 200
        assert cleared.json()["openai"]["configured"] is False
        assert key not in cleared.text
