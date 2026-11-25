from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Live keys on this box must never leak into unit tests (they would start a real mint).
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("OPENROUTER_API_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
src = ROOT / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _isolate_browser_platform_env(monkeypatch, tmp_path):
    """Tests must not inherit an access token from .env or other packages' dotenv loads."""
    monkeypatch.delenv("BROWSER_PLATFORM_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("BROWSER_PLATFORM_AUTO_LAUNCH_CHROME", "false")
    monkeypatch.setenv("BROWSER_PLATFORM_POMERADO_AUTO_LAUNCH_CHROMIUM", "false")
    monkeypatch.setenv("BROWSER_PLATFORM_OPEN_UI", "false")
    # Never read or write the developer's machine-local OpenAI key during tests.
    monkeypatch.setenv("BROWSER_PLATFORM_SECRETS_DIR", str(tmp_path / "secrets-root"))
