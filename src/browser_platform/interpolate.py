"""Replace {var} and {{var}} placeholders from workflow inputs / context."""

from __future__ import annotations

import re
from typing import Any

_BRACE = re.compile(r"\{\{?\s*([a-zA-Z_][\w]*)(?:\|([a-zA-Z0-9_]+))?\s*\}?\}")


def format_value(value: str, fmt: str | None) -> str:
    text = str(value or "")
    if not fmt:
        return text
    if fmt in {"slug", "hyphen"}:
        # Site convention: join words with hyphens ("England men" → "england-men").
        return path_slug(text)
    if fmt in {"slug_", "underscore"}:
        return re.sub(r"\s+", "_", text.strip()).lower()
    if fmt == "head":
        # First-word fallback when a site only uses the leading token ("England men" → "england").
        tokens = [tok for tok in re.split(r"\W+", text.strip()) if tok]
        return (tokens[0] if tokens else text).lower()
    return text


def interpolate(value: Any, variables: dict[str, Any] | None) -> Any:

    if not variables or not isinstance(value, str):
        return value

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        fmt = match.group(2) if match.lastindex and match.lastindex >= 2 else None
        if key in variables and variables[key] is not None:
            return format_value(str(variables[key]), fmt)
        return match.group(0)

    return _BRACE.sub(repl, value)


def path_slug(value: str) -> str:
    text = re.sub(r"\s+", "-", (value or "").strip())
    return text.lower()


def interpolate_url(url: Any, variables: dict[str, Any] | None) -> Any:
    """Interpolate a URL, then rewrite path segments that are input values into slugs.

    ``/teams/{team}`` with team=Arsenal becomes ``/teams/arsenal``. Wikipedia titles
    are left to ``normalize_wikipedia_url`` and are not slugged here.
    """
    if not isinstance(url, str):
        return interpolate(url, variables)
    updated = interpolate(url, variables)
    if not isinstance(updated, str) or not variables:
        return updated
    from urllib.parse import unquote, urlparse, urlunparse

    parsed = urlparse(updated)
    host = (parsed.netloc or "").lower()
    if "wikipedia.org" in host or "wikimedia.org" in host:
        return updated
    parts = parsed.path.split("/")
    changed = False
    raw_values = [str(v).strip() for v in variables.values() if v is not None and str(v).strip()]
    for i, part in enumerate(parts):
        if not part:
            continue
        decoded = unquote(part)
        for text in raw_values:
            candidates = {
                text,
                text.replace(" ", "_"),
                text.replace(" ", "-"),
                text.replace(" ", "+"),
            }
            lowered = {item.lower() for item in candidates}
            if decoded in candidates or decoded.lower() in lowered:
                slug = path_slug(text)
                if parts[i] != slug:
                    parts[i] = slug
                    changed = True
                break
    if not changed:
        return updated
    return urlunparse(parsed._replace(path="/".join(parts)))
