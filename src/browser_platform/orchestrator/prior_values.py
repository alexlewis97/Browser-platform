"""Infer an unstated input value from earlier runs of the same tool.

When the user names things (two clubs, say) but not the value a tool needs (the competition),
earlier runs of that tool show which value's page listed those things. Deterministic: the
value whose earlier pages contain every distinguishing word the user wrote, and only one
such value, is used."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from browser_platform.orchestrator.normalize_inputs import _similar, _tokens

_STOP = {"what", "when", "where", "which", "who", "whom", "whose", "with", "than", "that", "this", "there", "their",
         "they", "them", "have", "has", "more", "less", "most", "least", "above", "below", "higher", "lower", "about",
         "please", "show", "tell", "does", "from", "into", "over", "under", "between", "versus"}


def url_value_pattern(template: str, param: str) -> re.Pattern[str] | None:
    """Regex that captures ``param`` from a URL built from ``template`` ({name} or {name|filter})."""

    if not template or "{" + param not in template:
        return None
    parts = re.split(r"(\{[^{}]+\})", template)
    out = ""
    for part in parts:
        m = re.fullmatch(r"\{\s*([A-Za-z0-9_]+)(?:\|[^}]*)?\}", part)
        if m:
            out += "(?P<v>[^/?&#]+)" if m.group(1) == param and "(?P<v>" not in out else "[^/?&#]*"
        else:
            out += re.escape(part)
    try:
        return re.compile(out)
    except re.error:
        return None


def _run_texts(runs_dir: Path, workflow_id: str, pattern: re.Pattern[str], limit: int) -> dict[str, set[str]]:
    texts: dict[str, set[str]] = {}
    paths = sorted(Path(runs_dir).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("workflow_id") != workflow_id or data.get("status") not in {"succeeded", "succeeded_with_fallback"}:
            continue
        url = ""
        for step in data.get("steps") or []:
            url = str(((step or {}).get("output") or {}).get("url") or "") or url
            if url:
                break
        m = pattern.search(url) if url else None
        if not m:
            continue
        value = m.group("v")
        body = " ".join(str(v) for v in (data.get("outputs") or {}).values() if isinstance(v, str))
        texts.setdefault(value, set()).update(_tokens(body))
    return texts


def value_from_prior_runs(
    runs_dir: Path, workflow_id: str, template: str, param: str, user_words: str, *, limit: int = 400
) -> tuple[str, str] | None:
    """(value, the user's words it follows from), or None when earlier runs do not single one out."""

    pattern = url_value_pattern(template, param)
    if pattern is None:
        return None
    texts = _run_texts(runs_dir, workflow_id, pattern, limit)
    if len(texts) < 2:
        return None
    words = [w for w in dict.fromkeys(_tokens(user_words)) if len(w) >= 4 and w not in _STOP]

    def has(value: str, word: str) -> bool:
        toks = texts[value]
        return word in toks or any(len(t) >= 4 and _similar(word, t) >= 0.8 for t in toks)

    hits = {w: {v for v in texts if has(v, w)} for w in words}
    # Distinguishing words: on some earlier pages but not all of them.
    distinct = [w for w, vs in hits.items() if 0 < len(vs) < len(texts)]
    if not distinct:
        return None
    scores = {v: sum(1 for w in distinct if v in hits[w]) for v in texts}
    best = max(scores.values())
    winners = [v for v, sc in scores.items() if sc == best]
    if len(winners) != 1 or best < len(distinct):
        return None
    return winners[0], " ".join(w for w in distinct if winners[0] in hits[w])
