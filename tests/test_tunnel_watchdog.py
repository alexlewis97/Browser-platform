from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tunnel_watchdog.py"


def _load():
    spec = importlib.util.spec_from_file_location("tunnel_watchdog", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tunnel_watchdog"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_extract_urls_and_hosts_line():
    w = _load()
    log = """
    INF Requesting new quick Tunnel
    INF |  https://lung-casey-watching-don.trycloudflare.com                                         |
    INF Lost connection? no
    INF |  https://new-name-here.trycloudflare.com
    """
    assert w.extract_urls(log)[-1] == "https://new-name-here.trycloudflare.com"
    hosts = "127.0.0.1 localhost\n104.16.230.132 trycloudflare.com api.trycloudflare.com old-one.trycloudflare.com\n"
    updated = w.update_hosts_contents(hosts, "new-name-here.trycloudflare.com")
    assert "new-name-here.trycloudflare.com" in updated
    assert updated.startswith("127.0.0.1 localhost")
    # unchanged if already present
    again = w.update_hosts_contents(updated, "new-name-here.trycloudflare.com")
    assert again == updated


def test_death_markers_only_in_new_bytes(tmp_path, monkeypatch):
    w = _load()
    log = tmp_path / "tunnel.log"
    log.write_bytes(b"hello Tunnel not found before\n")
    monkeypatch.setattr(w, "TUNNEL_LOG", log)
    start = log.stat().st_size
    assert w.new_death_markers(start) == []
    log.write_bytes(log.read_bytes() + b"ERR Unauthorized: Tunnel not found\n")
    hits = w.new_death_markers(start)
    assert any("Tunnel not found" in h for h in hits)


def test_frontend_discovers_backend_json():
    source = (ROOT / "web" / "src" / "discover.ts").read_text()
    assert "/backend.json" in source
    assert "refreshDiscovery" in source
    api = (ROOT / "web" / "src" / "api.ts").read_text()
    assert "refreshDiscovery" in api
    assert "isNetworkError" in api
    bundle = " ".join(p.read_text(errors="ignore") for p in (ROOT / "web" / "site" / "assets").glob("*.js")) if (ROOT / "web" / "site").exists() else ""
    if bundle:
        assert "backend.json" in bundle
