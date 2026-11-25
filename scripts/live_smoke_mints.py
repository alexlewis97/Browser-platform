#!/usr/bin/env python3
"""Follow-up: Wikipedia deterministic replay (warm selectors) + Pomerado mints.

Merges into data/live_smoke_report.json. Never prints the API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import live_smoke as ls  # noqa: E402


async def main() -> int:
    from browser_platform.config import Settings
    from browser_platform.models.llm import LLMConfig
    from browser_platform.orchestrator.runner import WorkflowRunner
    from browser_platform.pomerado.adapter import PomeradoAdapter
    from browser_platform.store.file_store import FileStore

    key = ls._key()
    if not key:
        print(json.dumps({"skipped": True, "reason": "no key"}))
        return 1

    report_path = ls.REPORT
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    ls._assert_no_key(report, key)

    data_dir = ROOT / "data" / "live_smoke"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=data_dir, pomerado_host=ls.HOST, default_model="qwen/qwen3.7-flash")
    store = FileStore(data_dir)
    llm = LLMConfig(api_key=key, model="qwen/qwen3.7-flash")
    runner = WorkflowRunner(store, settings)

    proc, cdp = ls.start_chrome(data_dir / "chrome-profile-mints")
    pomerado = PomeradoAdapter(ls.HOST, root=data_dir / "integrations", cdp_url=cdp, model="qwen/qwen3.7-flash")
    try:
        wiki_def = ls._load_wf(ROOT / "examples" / "wikipedia_search.workflow.json")
        stored = store.create_workflow(wiki_def.model_copy(update={"name": "wikipedia_search_det_rerun"}))
        t0 = time.perf_counter()
        first = await runner.run(stored, cdp_url=cdp, variables={"term": "browser automation"}, llm=None)
        report["wikipedia_deterministic_retry"] = {
            **ls.summarize_run(first),
            "wall_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

        caps = await pomerado.capabilities()
        report["pomerado_capabilities"] = {
            "available": (caps.get("pomerado") or {}).get("available"),
            "createPomerado": (caps.get("pomerado") or {}).get("createPomerado"),
            "openrouter": caps.get("openrouter"),
            "cdp_passed_to_createPomerado": (caps.get("cdp") or {}).get("passed_to_createPomerado"),
        }

        mint_timeout = float(os.environ.get("POMERADO_LIVE_WAIT_S") or 720)
        t4 = time.perf_counter()
        example_mint = await pomerado.mint(
            name="example_com_reader",
            url="https://example.com",
            intent=(
                "Read-only: open https://example.com and return the page title and a short "
                "summary of the visible text. Do not click, type, log in, or change anything."
            ),
            effect="read",
            llm=llm,
            wait_s=mint_timeout,
        )
        report["pomerado_mint_example_com"] = {
            "status": example_mint.get("status"),
            "ok": example_mint.get("ok"),
            "latency_ms": example_mint.get("latency_ms"),
            "usage": example_mint.get("usage"),
            "output": example_mint.get("output"),
            "error": example_mint.get("error"),
            "directory": example_mint.get("directory"),
            "wall_ms": round((time.perf_counter() - t4) * 1000, 1),
            "pomerado": example_mint.get("pomerado"),
            "retried_after_chrome_launch_fix": True,
        }

        t5 = time.perf_counter()
        wiki_mint = await pomerado.mint(
            name="wikipedia_search_reader",
            url="https://en.wikipedia.org/wiki/Special:Search",
            intent=(
                "Read-only: on English Wikipedia, search for the term 'browser automation', "
                "open the first search result, and return the first paragraph of the article. "
                "Do not edit, log in, or change any settings."
            ),
            effect="read",
            input_values={"term": "browser automation"},
            llm=llm,
            wait_s=mint_timeout,
        )
        report["pomerado_mint_wikipedia"] = {
            "status": wiki_mint.get("status"),
            "ok": wiki_mint.get("ok"),
            "latency_ms": wiki_mint.get("latency_ms"),
            "usage": wiki_mint.get("usage"),
            "output": wiki_mint.get("output"),
            "error": wiki_mint.get("error"),
            "directory": wiki_mint.get("directory"),
            "wall_ms": round((time.perf_counter() - t5) * 1000, 1),
            "pomerado": wiki_mint.get("pomerado"),
            "retried_after_chrome_launch_fix": True,
        }
        report["followup_finished_at_ist"] = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    finally:
        await pomerado.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    ls._assert_no_key(report, key)
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                "wikipedia_deterministic_retry": (report.get("wikipedia_deterministic_retry") or {}).get("status"),
                "pomerado_mint_example_com": (report.get("pomerado_mint_example_com") or {}).get("status"),
                "pomerado_mint_wikipedia": (report.get("pomerado_mint_wikipedia") or {}).get("status"),
                "example_error": (report.get("pomerado_mint_example_com") or {}).get("error"),
                "wiki_mint_error": (report.get("pomerado_mint_wikipedia") or {}).get("error"),
            },
            default=str,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
