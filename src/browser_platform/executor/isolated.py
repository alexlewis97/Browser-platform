"""Launch a one-shot Chromium with its own CDP port and profile, then clean it up."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from browser_platform.chrome_launch import cdp_is_listening, launch_chrome_cdp


@dataclass
class IsolatedBrowser:
    cdp_url: str
    profile: Path
    proc: Any
    port: int


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def launch_isolated_sync(*, wait: float = 12.0) -> IsolatedBrowser:
    port = _free_port()
    # Portable temp dir (do not hardcode /tmp — breaks on Windows).
    profile = Path(tempfile.mkdtemp(prefix="bp-iso-"))
    cdp_url = f"http://127.0.0.1:{port}"
    proc = launch_chrome_cdp(cdp_url=cdp_url, profile_dir=profile)
    if proc is None:
        shutil.rmtree(profile, ignore_errors=True)
        raise RuntimeError(f"Could not launch an isolated Chromium on port {port}.")
    deadline = time.time() + wait
    while time.time() < deadline:
        if cdp_is_listening(cdp_url):
            return IsolatedBrowser(cdp_url=cdp_url, profile=profile, proc=proc, port=port)
        if proc.poll() is not None:
            shutil.rmtree(profile, ignore_errors=True)
            raise RuntimeError("Isolated Chromium exited before CDP became ready.")
        time.sleep(0.15)
    close_isolated_sync(IsolatedBrowser(cdp_url=cdp_url, profile=profile, proc=proc, port=port))
    raise RuntimeError(f"Timed out waiting for isolated Chromium on {cdp_url}.")


def close_isolated_sync(browser: IsolatedBrowser | None) -> None:
    if browser is None:
        return
    proc = browser.proc
    if proc is not None:
        try:
            if os.name != "nt" and hasattr(os, "killpg"):
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    proc.terminate()
            else:
                proc.terminate()
            try:
                proc.wait(timeout=4)
            except Exception:
                try:
                    if os.name != "nt" and hasattr(os, "killpg"):
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except Exception:
                    proc.kill()
                proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    shutil.rmtree(browser.profile, ignore_errors=True)


async def launch_isolated(*, wait: float = 12.0) -> IsolatedBrowser:
    return await asyncio.to_thread(launch_isolated_sync, wait=wait)


async def close_isolated(browser: IsolatedBrowser | None) -> None:
    await asyncio.to_thread(close_isolated_sync, browser)
