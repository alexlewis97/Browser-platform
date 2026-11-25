"""Dedicated long-lived Playwright Chromium server for official Pomerado --endpoint."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from browser_platform.chrome_launch import find_playwright_chromium, _posix_chrome_candidates, _windows_chrome_candidates

logger = logging.getLogger("browser_platform.pomerado")

DEFAULT_ENDPOINT_FILE = "pomerado-browser.endpoint"


@dataclass
class PomeradoChromium:
    endpoint: str
    profile_dir: Path
    pid: int | None
    launched: bool
    # Keep cdp_url alias unused; official host refuses CDP.
    cdp_url: str | None = None

    def healthy(self) -> bool:
        return bool(self.endpoint.startswith("ws"))


def profile_dir_for(data_dir: Path, configured: Path | None = None) -> Path:
    if configured is not None:
        return Path(configured)
    return Path(data_dir) / "pomerado-chromium-profile"


def endpoint_file_for(profile_dir: Path) -> Path:
    return Path(profile_dir) / DEFAULT_ENDPOINT_FILE


def read_endpoint(path: Path) -> str | None:
    try:
        text = path.read_text().strip()
    except Exception:
        return None
    return text if text.startswith("ws") else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False



def _find_executable() -> Path | None:
    if os.name == "nt":
        candidates = _windows_chrome_candidates()
    else:
        candidates = _posix_chrome_candidates()
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return find_playwright_chromium()

def ensure_pomerado_chromium(
    *,
    cdp_url: str | None = None,  # ignored; official Pomerado rejects CDP
    profile_dir: Path,
    auto_launch: bool = True,
    headed: bool | None = None,
    host_root: Path | None = None,
) -> PomeradoChromium:
    """Reuse or start a Playwright launchServer. The process is left running."""

    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    endpoint_path = endpoint_file_for(profile_dir)
    pid_path = profile_dir / "browser.pid"
    existing = read_endpoint(endpoint_path)
    pid = _read_pid(pid_path)
    if existing and pid and _pid_alive(pid):
        return PomeradoChromium(endpoint=existing, profile_dir=profile_dir, pid=pid, launched=False)

    if not auto_launch:
        return PomeradoChromium(endpoint=existing or "", profile_dir=profile_dir, pid=pid, launched=False)

    script = (host_root or Path(__file__).resolve().parents[3] / "node_host") / "src" / "persistent-browser.mjs"
    if not script.is_file():
        raise RuntimeError(f"missing persistent browser script at {script}")

    if headed is None:
        if os.name == "nt":
            headed = os.environ.get("BROWSER_PLATFORM_POMERADO_HEADED", "1") != "0"
        else:
            headed = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    env = {k: v for k, v in os.environ.items() if "KEY" not in k.upper() and "TOKEN" not in k.upper() and "SECRET" not in k.upper()}
    env["PATH"] = os.environ.get("PATH", "")
    env["HOME"] = os.environ.get("HOME", "")
    env["POMERADO_PROFILE_DIR"] = str(profile_dir)
    env["POMERADO_ENDPOINT_FILE"] = str(endpoint_path)
    executable = _find_executable()
    if executable:
        env["POMERADO_BROWSER_EXECUTABLE"] = str(executable)
    if headed:
        env["POMERADO_HEADED"] = "1"
    if endpoint_path.is_file():
        endpoint_path.unlink()
    log_path = profile_dir / "browser.log"
    log_file = open(log_path, "ab")
    proc = subprocess.Popen(
        ["node", str(script)],
        stdout=log_file,
        stderr=log_file,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=env,
        cwd=str(script.parent.parent),
    )
    pid_path.write_text(str(proc.pid) + "\n")
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Pomerado Playwright server exited immediately (code {proc.returncode})")
        endpoint = read_endpoint(endpoint_path)
        if endpoint:
            logger.info("Pomerado Playwright server ready (pid %s)", proc.pid)
            return PomeradoChromium(endpoint=endpoint, profile_dir=profile_dir, pid=proc.pid, launched=True)
        time.sleep(0.15)
    raise RuntimeError("Pomerado Playwright server did not publish a websocket endpoint")


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except Exception:
        return None
