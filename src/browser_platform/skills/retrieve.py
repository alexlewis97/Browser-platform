"""Top-k skill retrieval: BM25 over manifests, fused with cached embeddings when available."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter, OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any

from browser_platform.skills.manifest import search_text, words

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 256
RRF_K = 60


def bm25_scores(query: str, docs: list[str], *, k1: float = 1.4, b: float = 0.75) -> list[float]:
    tokenized = [words(doc) for doc in docs]
    q = list(dict.fromkeys(words(query)))
    if not q or not docs:
        return [0.0] * len(docs)
    n = len(docs)
    avg = sum(len(t) for t in tokenized) / max(n, 1) or 1.0
    df = Counter()
    for toks in tokenized:
        df.update(set(toks))
    scores = []
    for toks in tokenized:
        tf = Counter(toks)
        score = 0.0
        for term in q:
            # Light prefix match so "fixture" hits "fixtures" and "sort" hits "sorting".
            freq = tf.get(term, 0)
            if not freq and len(term) >= 4:
                freq = 0.5 * sum(v for t, v in tf.items() if t.startswith(term) or term.startswith(t) and len(t) >= 4)
            if not freq:
                continue
            idf = math.log(1 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            score += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * len(toks) / avg))
        scores.append(score)
    return scores


class Embedder:
    """OpenAI small embeddings with an on-disk cache. Silently disabled without a key."""

    def __init__(self, cache_path: Path | None = None):
        self.cache_path = cache_path
        self._cache: dict[str, list[float]] | None = None
        self._queries: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = Lock()
        self.disabled_reason: str | None = None

    @property
    def enabled(self) -> bool:
        return bool((os.environ.get("OPENAI_API_KEY") or "").strip()) and os.environ.get("BROWSER_PLATFORM_SKILL_EMBEDDINGS", "1") != "0"

    def _load(self) -> dict[str, list[float]]:
        if self._cache is None:
            self._cache = {}
            if self.cache_path and self.cache_path.is_file():
                try:
                    self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
                except Exception:
                    self._cache = {}
        return self._cache

    def _save(self) -> None:
        if not self.cache_path or self._cache is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache), encoding="utf-8")
        tmp.replace(self.cache_path)

    async def _embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        import httpx

        key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        async with httpx.AsyncClient(timeout=8) as client:
            res = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": EMBED_MODEL, "input": texts, "dimensions": EMBED_DIMS},
            )
            res.raise_for_status()
            body = res.json()
        vecs = [row["embedding"] for row in sorted(body.get("data") or [], key=lambda r: r.get("index", 0))]
        return vecs, int((body.get("usage") or {}).get("total_tokens") or 0)

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]

    async def docs(self, texts: list[str]) -> tuple[list[list[float]] | None, int]:
        cache = self._load()
        missing = [t for t in texts if self._key(t) not in cache]
        tokens = 0
        if missing:
            vecs, tokens = await self._embed(missing)
            with self._lock:
                for text, vec in zip(missing, vecs):
                    cache[self._key(text)] = [round(x, 5) for x in vec]
                self._save()
        return [cache[self._key(t)] for t in texts], tokens

    async def query(self, text: str) -> tuple[list[float], int]:
        key = self._key(text)
        if key in self._queries:
            self._queries.move_to_end(key)
            return self._queries[key], 0
        vecs, tokens = await self._embed([text])
        self._queries[key] = vecs[0]
        while len(self._queries) > 256:
            self._queries.popitem(last=False)
        return vecs[0], tokens


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


async def rank_skills(
    query: str,
    manifests: list[dict[str, Any]],
    *,
    k: int = 6,
    embedder: Embedder | None = None,
    prior: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Return {"skills": [manifest...], "method": ..., "embed_tokens": n, "scores": [...]}"""

    if not manifests:
        return {"skills": [], "method": "none", "embed_tokens": 0, "scores": []}
    docs = [search_text(m) for m in manifests]
    lexical = bm25_scores(query, docs)
    order_lex = sorted(range(len(docs)), key=lambda i: lexical[i], reverse=True)
    fused: dict[int, float] = {}
    for rank, i in enumerate(order_lex):
        if lexical[i] > 0:
            fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)
    method = "bm25"
    embed_tokens = 0
    dense: list[float] = []
    if embedder is not None and embedder.enabled:
        try:
            doc_vecs, t1 = await embedder.docs(docs)
            q_vec, t2 = await embedder.query(query)
            embed_tokens = t1 + t2
            dense = [_cos(q_vec, v) for v in doc_vecs or []]
            order_dense = sorted(range(len(docs)), key=lambda i: dense[i], reverse=True)
            for rank, i in enumerate(order_dense):
                fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)
            method = "hybrid"
        except Exception as exc:  # network or quota: lexical only
            embedder.disabled_reason = type(exc).__name__
            dense = []
    if prior:
        # Reliability prior from run history (0..1): nudges, never overrides, relevance.
        for i in list(fused):
            fused[i] *= 0.85 + 0.3 * float(prior.get(str(manifests[i].get("id")), 0.5))
    if not fused:
        # Nothing matched lexically and no embeddings: give the planner a small default slate.
        chosen = list(range(min(k, len(manifests))))
    else:
        chosen = sorted(fused, key=lambda i: fused[i], reverse=True)[:k]
    return {
        "skills": [manifests[i] for i in chosen],
        "method": method,
        "embed_tokens": embed_tokens,
        "scores": [
            {
                "slug": manifests[i].get("slug"),
                "bm25": round(lexical[i], 3),
                "dense": round(dense[i], 3) if dense else None,
                "rrf": round(fused.get(i, 0.0), 5),
            }
            for i in chosen
        ],
    }
