"""Bump workflow-use string versions after a repair write-back."""

from __future__ import annotations

import re


def bump_version(version: str) -> str:
    text = str(version).strip() or "1"
    parts = text.split(".")
    nums = []
    for part in parts:
        if re.fullmatch(r"\d+", part):
            nums.append(int(part))
        else:
            return f"{text}+repaired"
    if not nums:
        return "1"
    nums[-1] += 1
    return ".".join(str(n) for n in nums)
