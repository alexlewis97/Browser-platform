#!/usr/bin/env python3
"""Mint read-only public tools with the OpenRouter-adapted Pomerado host, then run_artifact.

Never prints the OpenRouter API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import live_smoke as ls  # noqa: E402

PRIMARY = "qwen/qwen3.7-flash"
CODER = "qwen/qwen3-coder-30b-a3b-instruct"
REPORT = ROOT / "data" / "live_mint_pomerado_report.json"
WAIT_S = float(os.environ.get("POMERADO_LIVE_WAIT_S") or 900)
BUDGET_USD = float(os.environ.get("POMERADO_LIVE_BUDGET_USD") or 1.0)
LIVE_ONLY = (os.environ.get("POMERADO_LIVE_ONLY") or "").strip().lower()
SPEND = ROOT / "data" / "live_round_spend.json"
IST = ZoneInfo("Asia/Jerusalem")
PRIOR_SPEND_USD = float(os.environ.get("POMERADO_PRIOR_SPEND_USD") or 0.110968)
ROUND_CAP_USD = float(os.environ.get("POMERADO_ROUND_CAP_USD") or 1.0)

def _now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _brief_mint(result: dict) -> dict:
    output = result.get("output") or {}
    return {
        "status": result.get("status"),
        "ok": result.get("ok"),
        "error": result.get("error"),
        "latency_ms": result.get("latency_ms"),
        "wall_ms": result.get("wall_ms"),
        "usage": result.get("usage"),
        "directory": result.get("directory"),
        "model": result.get("model"),
        "build": output.get("build"),
        "summary": output.get("summary"),
        "publicationRef": output.get("publicationRef"),
        "stopReason": output.get("stopReason"),
        "hostFailure": output.get("hostFailure"),
        "diagnostics": (output.get("extra_keys") or {}).get("diagnostics"),
        "has_artifact": bool((output.get("artifact") or {}).get("has_artifact")),
        "artifact": output.get("artifact"),
    }


def _usage_cost(result) -> float:
    usage = (result or {}).get("usage") or {}
    try:
        return float(usage.get("cost_usd") or 0)
    except (TypeError, ValueError):
        return 0.0


async def mint_with_fallback(adapter_factory, *, name, url, intent, input_values, key, models, budget_left):
    from browser_platform.models.llm import LLMConfig
    from browser_platform.pomerado.adapter import PomeradoAdapter

    attempts = []
    spent = 0.0
    for model in models:
        if budget_left - spent < 0.05:
            attempts.append({"status": "skipped", "error": "budget exhausted", "model": model, "usage": {"cost_usd": 0}})
            break
        adapter: PomeradoAdapter = adapter_factory(model)
        llm = LLMConfig(api_key=key, model=model)
        t0 = time.perf_counter()
        try:
            result = await adapter.mint(
                name=name,
                url=url,
                intent=intent,
                effect="read",
                input_values=input_values,
                llm=llm,
                wait_s=WAIT_S,
            )
        finally:
            await adapter.close()
        result["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        result["model"] = model
        brief = _brief_mint(result)
        attempts.append(brief)
        spent += _usage_cost(result)
        print(json.dumps({"event": "mint_attempt_finished", "name": name, "attempt": brief, "spent_usd": round(spent, 6)}, default=str), flush=True)
        published = result.get("ok") is True or (result.get("output") or {}).get("build") == "published"
        if published:
            return result, attempts, spent
        status = result.get("status")
        if status in {"running", "queued"}:
            # Do not stack a second long mint on a still-running job.
            return None, attempts, spent
        # Finished but unpublished (transport error, incomplete build, model failure): try the next model.
    return None, attempts, spent


async def run_published(adapter, *, directory, url, intent, input_values=None):
    t0 = time.perf_counter()
    result = await adapter.run_artifact(
        directory,
        input_values=input_values or {},
        url=url,
        effect="read",
        intent=intent,
        timeout=180,
    )
    result["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result


async def run_via_tier(store, settings, *, directory, url, name, intent, input_values=None):
    from browser_platform.models.workflow import WorkflowDefinition, WorkflowStep
    from browser_platform.orchestrator.runner import WorkflowRunner
    from browser_platform.pomerado.adapter import PomeradoAdapter

    adapter = PomeradoAdapter(ls.HOST, root=Path(directory).parent, model=PRIMARY)
    runner = WorkflowRunner(store, settings, pomerado=adapter)
    stored = store.create_workflow(
        WorkflowDefinition(
            name=name,
            description=intent,
            version="1",
            pomerado_artifact=str(directory),
            pomerado_input={"url": url, "intent": intent, "effect": "read", **(input_values or {})},
            steps=[WorkflowStep(type="extract", extractionGoal="unused if pomerado succeeds")],
        )
    )
    # CDP is unused if Pomerado satisfies the workflow, but the runner requires a url.
    dummy = "http://127.0.0.1:9"
    t0 = time.perf_counter()
    try:
        run = await runner.run(stored, cdp_url=dummy)
        return {**ls.summarize_run(run), "wall_ms": round((time.perf_counter() - t0) * 1000, 1)}
    finally:
        await adapter.close()


async def main() -> int:
    from browser_platform.config import Settings
    from browser_platform.pomerado.adapter import PomeradoAdapter
    from browser_platform.store.file_store import FileStore

    key = ls._key()
    if not key:
        print(json.dumps({"skipped": True, "reason": "OPENROUTER_API_KEY is not set"}))
        return 1

    data_dir = ROOT / "data" / "live_mint"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=data_dir, pomerado_host=ls.HOST, default_model=PRIMARY)
    store = FileStore(data_dir)

    def make_adapter(model: str) -> PomeradoAdapter:
        return PomeradoAdapter(ls.HOST, root=data_dir / "integrations", model=model)

    report = {
        "started_at_ist": _now(),
        "primary_model": PRIMARY,
        "coder_fallback_model": CODER,
        "key_present": True,
        "key_length": len(key),
        "budget_usd": BUDGET_USD,
        "spend_usd": 0,
    }

    example_intent = (
        "Read-only: open https://example.com and return the page title and a short "
        "summary of the visible text. Do not click, type, log in, or change anything."
    )
    wiki_intent = (
        "Read-only: on English Wikipedia, search for the term 'browser automation', "
        "open the first search result, and return the first paragraph of the article. "
        "Do not edit, log in, or change any settings."
    )

    budget_left = BUDGET_USD
    example_result, example_attempts, example_spent = await mint_with_fallback(
        make_adapter,
        name="example_com_reader",
        url="https://example.com",
        intent=example_intent,
        input_values={},
        key=key,
        models=[PRIMARY, CODER],
        budget_left=budget_left,
    )
    budget_left = max(0.0, budget_left - example_spent)
    report["spend_usd"] = round(BUDGET_USD - budget_left, 6)
    report["example_com_mint_attempts"] = example_attempts
    report["example_com_mint"] = _brief_mint(example_result) if example_result else example_attempts[-1]

    wiki_result, wiki_attempts, wiki_spent = None, [], 0.0
    if LIVE_ONLY in {"", "all", "both"}:
        wiki_result, wiki_attempts, wiki_spent = await mint_with_fallback(
            make_adapter,
            name="wikipedia_search_reader",
            url="https://en.wikipedia.org/wiki/Special:Search",
            intent=wiki_intent,
            input_values={"term": "browser automation"},
            key=key,
            models=[PRIMARY, CODER],
            budget_left=budget_left,
        )
        budget_left = max(0.0, budget_left - wiki_spent)
        report["spend_usd"] = round(BUDGET_USD - budget_left, 6)
        report["wikipedia_mint_attempts"] = wiki_attempts
        report["wikipedia_mint"] = _brief_mint(wiki_result) if wiki_result else wiki_attempts[-1]
    else:
        report["wikipedia_mint"] = {"status": "skipped", "reason": f"POMERADO_LIVE_ONLY={LIVE_ONLY}"}
        report["wikipedia_mint_attempts"] = []

    runner_adapter = make_adapter(PRIMARY)
    try:
        if example_result and example_result.get("directory"):
            run = await run_published(
                runner_adapter,
                directory=example_result["directory"],
                url="https://example.com",
                intent=example_intent,
            )
            report["example_com_run_artifact"] = {
                "ok": run.get("ok"),
                "status": run.get("status"),
                "error": run.get("error"),
                "latency_ms": run.get("latency_ms"),
                "wall_ms": run.get("wall_ms"),
                "output": run.get("output"),
            }
            report["example_com_pomerado_tier"] = await run_via_tier(
                store,
                settings,
                directory=example_result["directory"],
                url="https://example.com",
                name="example_com_pomerado_run",
                intent=example_intent,
            )
        if wiki_result and wiki_result.get("directory"):
            run = await run_published(
                runner_adapter,
                directory=wiki_result["directory"],
                url="https://en.wikipedia.org/wiki/Special:Search",
                intent=wiki_intent,
                input_values={"term": "browser automation"},
            )
            report["wikipedia_run_artifact"] = {
                "ok": run.get("ok"),
                "status": run.get("status"),
                "error": run.get("error"),
                "latency_ms": run.get("latency_ms"),
                "wall_ms": run.get("wall_ms"),
                "output": run.get("output"),
            }
            report["wikipedia_pomerado_tier"] = await run_via_tier(
                store,
                settings,
                directory=wiki_result["directory"],
                url="https://en.wikipedia.org/wiki/Special:Search",
                name="wikipedia_pomerado_run",
                intent=wiki_intent,
                input_values={"term": "browser automation"},
            )
    finally:
        await runner_adapter.close()

    report["finished_at_ist"] = _now()
    this_spend = round(BUDGET_USD - budget_left, 6)
    report["spend_usd"] = this_spend
    spend = {"round_cap_usd": ROUND_CAP_USD, "prior_spend_usd": PRIOR_SPEND_USD, "items": []}
    if SPEND.exists():
        try:
            spend = json.loads(SPEND.read_text())
        except Exception:
            pass
    spend["round_cap_usd"] = ROUND_CAP_USD
    spend["prior_spend_usd"] = PRIOR_SPEND_USD
    spend["items"] = [item for item in spend.get("items") or [] if item.get("name") != "pomerado_example_com_remint"]
    spend["items"].append({"name": "pomerado_example_com_remint", "cost_usd": this_spend, "at": report["finished_at_ist"]})
    spend["total_spend_usd"] = round(PRIOR_SPEND_USD + sum(float(i.get("cost_usd") or 0) for i in spend["items"]), 6)
    spend["remaining_usd"] = round(ROUND_CAP_USD - spend["total_spend_usd"], 6)
    report["round_spend"] = spend
    ls._assert_no_key(spend, key)
    SPEND.write_text(json.dumps(spend, indent=2) + "\n")
    ls._assert_no_key(report, key)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                "report": str(REPORT),
                "example_com_mint": report.get("example_com_mint"),
                "wikipedia_mint": report.get("wikipedia_mint"),
                "example_com_run_artifact": {
                    k: (report.get("example_com_run_artifact") or {}).get(k)
                    for k in ("ok", "status", "error", "latency_ms", "wall_ms")
                },
                "wikipedia_run_artifact": {
                    k: (report.get("wikipedia_run_artifact") or {}).get(k)
                    for k in ("ok", "status", "error", "latency_ms", "wall_ms")
                },
                "example_com_pomerado_tier": (report.get("example_com_pomerado_tier") or {}).get("status"),
                "wikipedia_pomerado_tier": (report.get("wikipedia_pomerado_tier") or {}).get("status"),
                "spend_usd": report.get("spend_usd"),
                "round_spend": report.get("round_spend"),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
