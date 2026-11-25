#!/usr/bin/env python3
"""Build /workspace/browser-platform-release/browser-platform-windows.zip (never a dist/ folder)."""

from __future__ import annotations

import os
import re
import stat
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = Path("/workspace/browser-platform-release")
ZIP_NAME = "browser-platform-windows.zip"

EXCLUDE_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".coverage",
    ".vercel",
    "dist",
    "build",
    "out",
    "coverage",
    "target",
    "data",
    "chrome-profile",
    ".cursor",
}
EXCLUDE_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".DS_Store",
    "backend.json",
    ".install-stamp",
}
# Live-looking keys. Placeholders in tests/UI (fake, not-used, example, xxx, all-same) are allowed.
_LIVE_OR = re.compile(r"sk-or-v1-[A-Fa-f0-9]{32,}")
_LIVE_OA = re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")
_PLACEHOLDER = re.compile(r"(fake|not-used|not_used|example|placeholder|your-key|your_key|xxx|aaaaaa)", re.IGNORECASE)
_OIDC = re.compile(r"VERCEL_OIDC_TOKEN\s*=\s*\S+")
_OR_ASSIGN = re.compile(r"(OPENROUTER_API_KEY|OPENAI_API_KEY)\s*=\s*\S+")

INCLUDE_TOP = [
    "src",
    "examples",
    "scripts",
    "web",
    "node_host",
    "README.md",
    "LICENSE",
    "pyproject.toml",
    ".env.example",
    ".gitignore",
    "install.ps1",
    "start.bat",
    "update-url.txt",
    "release-id.txt",
]


EXCLUDE_RELATIVE = {
    "scripts/tunnel_watchdog.py",
    "tests/test_tunnel_watchdog.py",
    "web/public/backend.json",
    "web/site/backend.json",
}


def _excluded_dir(name: str) -> bool:
    return name in EXCLUDE_DIR_NAMES or name.endswith(".egg-info")


def iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for top in INCLUDE_TOP:
        path = root / top
        if not path.exists():
            continue
        if path.is_file():
            files.append(path)
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if not _excluded_dir(d)]
            for name in filenames:
                if name in EXCLUDE_FILE_NAMES or name.endswith(".pyc"):
                    continue
                lower = name.lower()
                if lower.startswith(".env") and lower != ".env.example":
                    continue
                path = Path(dirpath) / name
                rel = path.relative_to(root).as_posix()
                if rel in EXCLUDE_RELATIVE:
                    continue
                files.append(path)
    return files


def arcname(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    return f"browser-platform/{rel}"


def looks_secret(path: Path) -> str | None:
    name = path.name.lower()
    if name.startswith(".env") and name != ".env.example":
        return f"env file {path}"
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".woff", ".woff2", ".ico", ".zip"}:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    if path.suffix.lower() in {".md", ".example"} or path.name == ".env.example":
        return None
    for match in list(_LIVE_OR.finditer(text)) + list(_LIVE_OA.finditer(text)):
        token = match.group(0)
        if _PLACEHOLDER.search(token):
            continue
        body = token.split("-", 1)[-1]
        if body and len(set(body.lower())) <= 2:
            continue
        return f"{path} contains a live-looking API key"
    for match in list(_OIDC.finditer(text)) + list(_OR_ASSIGN.finditer(text)):
        blob = match.group(0)
        rhs = blob.split("=", 1)[-1].strip().strip('"\'')
        if not rhs or _PLACEHOLDER.search(blob):
            continue
        return f"{path} contains a secret assignment"
    return None


def main() -> int:
    missing = [name for name in ("install.ps1", "start.bat", "pyproject.toml") if not (ROOT / name).exists()]
    if missing:
        print("missing required files:", missing, file=sys.stderr)
        return 1
    site = ROOT / "web" / "site" / "index.html"
    if not site.is_file():
        print("web/site is not built; run: cd web && npm run build", file=sys.stderr)
        return 1
    release_id = ROOT / "release-id.txt"
    release_id.write_text(__import__("datetime").datetime.now().strftime("%Y-%m-%d") + "-windows\n", encoding="utf-8")
    files = iter_files(ROOT)
    if release_id not in files:
        files.append(release_id)
    problems = [looks_secret(path) for path in files]
    problems = [p for p in problems if p]
    if problems:
        print("refusing to pack secrets:", file=sys.stderr)
        for item in problems:
            print(" -", item, file=sys.stderr)
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUT_DIR / ZIP_NAME
    if dest.exists():
        dest.unlink()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files, key=lambda p: p.as_posix()):
            zf.write(path, arcname=arcname(path, ROOT))
    size = dest.stat().st_size
    print(f"wrote {dest} ({size} bytes, {len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
