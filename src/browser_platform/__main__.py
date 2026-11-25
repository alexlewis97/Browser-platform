"""python -m browser_platform"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from browser_platform.app import create_app
from browser_platform.config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="browser-platform orchestrator")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--open-ui",
        action="store_true",
        help="Open http://<host>:<port>/ in the default browser after start (used by start.bat).",
    )
    parser.add_argument("--no-chrome-launch", action="store_true", help="Do not auto-launch Chrome for CDP.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    settings = load_settings(root)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.open_ui:
        settings.open_ui = True
    if args.no_chrome_launch:
        settings.auto_launch_chrome = False
    app = create_app(settings, root=root)
    uvicorn.run(
        app,
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
