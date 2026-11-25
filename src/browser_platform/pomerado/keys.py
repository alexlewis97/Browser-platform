"""Detect a live-looking API key without logging or returning the value."""

from __future__ import annotations


_BLOCKLIST = ("fake", "not-used", "not_used", "example", "placeholder", "your-key", "your_key", "xxx")


def looks_like_live_key(key: str | None) -> bool:
    if not key or len(key) < 24:
        return False
    lowered = key.lower()
    return not any(token in lowered for token in _BLOCKLIST)
