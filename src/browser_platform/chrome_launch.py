"""Find and auto-launch the user's Chrome (or Playwright Chromium) for CDP."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_CDP_PORT = 9222


def cdp_port(url: str | None) -> tuple[str, int]:
    parsed = urlparse(url or f"http://127.0.0.1:{DEFAULT_CDP_PORT}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or DEFAULT_CDP_PORT
    return host, port


def cdp_is_listening(url: str | None, timeout: float = 0.35) -> bool:
    host, port = cdp_port(url)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def default_profile_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "BrowserPlatform" / "chrome-profile"
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "BrowserPlatform" / "chrome-profile"


def _windows_chrome_candidates() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    pf = os.environ.get("PROGRAMFILES") or r"C:\Program Files"
    pf86 = os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)"
    return [
        Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]


def _posix_chrome_candidates() -> list[Path]:
    names = [
        "google-chrome-stable",
        "google-chrome",
        "chromium-browser",
        "chromium",
        "chrome",
    ]
    found: list[Path] = []
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            found.append(Path(resolved))
    found.extend(
        [
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/chromium-browser"),
            Path("/usr/bin/chromium"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    )
    return found


def find_playwright_chromium() -> Path | None:
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    roots: list[Path] = []
    if env and env != "0":
        roots.append(Path(env))
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        roots.append(Path(local) / "ms-playwright")
    else:
        roots.append(Path.home() / ".cache" / "ms-playwright")
        roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
        roots.append(Path("/ms-playwright"))
    patterns = (
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-win/headless_shell.exe",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-linux-arm64/chrome",
        "chromium-*/chrome-linux*/chrome",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
        "chromium_headless_shell-*/chrome-linux-arm64/headless_shell",
        "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        "chromium-*/chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
    )
    matches: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            matches.extend(path for path in root.glob(pattern) if path.is_file())
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def find_chrome() -> Path | None:
    """Prefer the user's installed Chrome; fall back to Playwright's Chromium."""

    if os.name == "nt":
        for candidate in _windows_chrome_candidates():
            if candidate.is_file():
                return candidate
        which = shutil.which("chrome") or shutil.which("chrome.exe")
        if which:
            return Path(which)
    else:
        for candidate in _posix_chrome_candidates():
            if candidate.is_file():
                return candidate
    return find_playwright_chromium()



def container_chrome_args() -> list[str]:
    """Extra Chromium flags for Docker / explicit headless mode. Never applied on Windows desktops automatically."""
    extra = [part for part in os.environ.get("BROWSER_PLATFORM_CHROME_ARGS", "").split() if part]
    flagged = os.environ.get("BROWSER_PLATFORM_CHROME_HEADLESS", "").strip().lower() in {"1", "true", "yes"}
    in_docker = Path("/.dockerenv").exists()
    if not (flagged or in_docker):
        return extra
    defaults = [
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-software-rasterizer",
        "--disable-extensions",
    ]
    have = set(extra)
    return extra + [flag for flag in defaults if flag not in have]


def launch_chrome_cdp(
    *,
    cdp_url: str = "http://127.0.0.1:9222",
    profile_dir: Path | None = None,
    binary: Path | None = None,
) -> subprocess.Popen | None:
    """Start Chrome with a persistent profile if the CDP port is free. Never kills an existing browser."""

    if cdp_is_listening(cdp_url):
        return None
    binary = binary or find_chrome()
    if binary is None:
        return None
    _, port = cdp_port(cdp_url)
    profile = Path(profile_dir) if profile_dir else default_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        str(binary),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
        *container_chrome_args(),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0x00000008
        )
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=(os.name != "nt"),
        creationflags=creationflags,
        start_new_session=(os.name != "nt"),
    )


def maybe_launch_cdp(settings: object) -> subprocess.Popen | None:
    if not bool(getattr(settings, "auto_launch_chrome", True)):
        return None
    cdp_url = getattr(settings, "default_cdp_url", None) or "http://127.0.0.1:9222"
    profile = getattr(settings, "chrome_profile_dir", None)
    return launch_chrome_cdp(cdp_url=cdp_url, profile_dir=Path(profile) if profile else None)
