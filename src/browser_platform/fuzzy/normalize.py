"""Normalize labels for fuzzy matching: diacritics, Hebrew niqqud, RTL marks, name order."""

from __future__ import annotations

import re
import unicodedata

# Hebrew points / cantillation, Arabic harakat, and common bidi marks.
_HEBREW_POINTS = {(c) for c in range(0x0591, 0x05C8)}
_ARABIC_MARKS = {(c) for c in range(0x064B, 0x0660)} | set(range(0x0670, 0x0671)) | set(range(0x06D6, 0x06ED))
_BIDI_MARKS = {0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069}
_PUNCT_TO_SPACE = re.compile(r"[,;|/\-_·•]+")
_WS = re.compile(r"\s+")
_APOS = re.compile(r"['’`´]")


def strip_marks(text: str) -> str:
    """NFKD-decompose and drop combining marks, niqqud, and bidi controls."""

    decomposed = unicodedata.normalize("NFKD", text)
    chars: list[str] = []
    for ch in decomposed:
        code = ord(ch)
        if unicodedata.combining(ch):
            continue
        if code in _HEBREW_POINTS or code in _ARABIC_MARKS or code in _BIDI_MARKS:
            continue
        chars.append(ch)
    return "".join(chars)


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", str(text))
    value = strip_marks(value)
    value = _APOS.sub("", value)
    value = _PUNCT_TO_SPACE.sub(" ", value)
    value = _WS.sub(" ", value).strip().casefold()
    return value


def name_variants(text: str | None) -> set[str]:
    """Normalized forms including swapped 'Last, First' / 'First Last' token orders."""

    raw = str(text or "").strip()
    variants: set[str] = set()
    norm = normalize_text(raw)
    if norm:
        variants.add(norm)
    if "," in raw:
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        if len(parts) == 2:
            swapped = normalize_text(f"{parts[1]} {parts[0]}")
            if swapped:
                variants.add(swapped)
    tokens = norm.split()
    if len(tokens) >= 2:
        variants.add(" ".join(reversed(tokens)))
        variants.add(tokens[-1] + " " + " ".join(tokens[:-1]))
        variants.add(" ".join(tokens[1:]) + " " + tokens[0])
        variants.add(" ".join(sorted(tokens)))
    return variants or {""}
