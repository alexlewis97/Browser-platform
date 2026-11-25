#!/usr/bin/env python3
"""Live public-site smoke: Pomerado mint, workflow-use replay, browser-use repair.

Never prints the OpenRouter API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CHROME = Path("/usr/bin/google-chrome")
REPORT = ROOT / "data" / "live_smoke_report.json"
HOST = ROOT / "node_host" / "src" / "index.mjs"


def _key() -> str:
    return os.environ.get("OPENROUTER_API_KEY") or ""


def _assert_no_key(payload, key: str) -> None:
    if not key:
        return
    blob = json.dumps(payload, default=str)
    if key in blob:
        raise RuntimeError("refusing to write a report that contains the API key")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_cdp(port: int, timeout: float = 25) -> str:
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return f"http://127.0.0.1:{port}"
        except Exception as exc:
            last = exc
            time.sleep(0.15)
    raise RuntimeError(f"Chrome CDP did not come up on {port}: {last}")


def start_chrome(profile: Path) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(CHROME),
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--remote-debugging-port={port}",
            f"--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        cdp = _wait_cdp(port)
    except Exception:
        proc.terminate()
        raise
    return proc, cdp


def summarize_run(run) -> dict:
    steps = []
    by_tier: dict[str, dict] = {}
    for s in run.steps:
        tier = s.tier or "unknown"
        rec = by_tier.setdefault(
            tier,
            {"count": 0, "latency_ms": 0.0, "tokens_in": 0, "tokens_out": 0, "success": 0},
        )
        rec["count"] += 1
        rec["latency_ms"] += float(s.latency_ms or 0)
        rec["tokens_in"] += int(s.tokens_in or 0)
        rec["tokens_out"] += int(s.tokens_out or 0)
        rec["success"] += int(bool(s.success))
        out = s.output
        if isinstance(out, str) and len(out) > 280:
            out = out[:280] + "…"
        steps.append(
            {
                "index": s.step_index,
                "type": s.step_type,
                "tier": tier,
                "success": s.success,
                "latency_ms": round(float(s.latency_ms or 0), 1),
                "tokens_in": int(s.tokens_in or 0),
                "tokens_out": int(s.tokens_out or 0),
                "error": s.error,
                "output": out,
            }
        )
    outputs = run.outputs or {}
    preview = {}
    for k, v in outputs.items():
        preview[k] = (v[:400] + "…") if isinstance(v, str) and len(v) > 400 else v
    return {
        "status": run.status,
        "error": run.error,
        "workflow_version": run.workflow_version,
        "steps": steps,
        "by_tier": {k: {**v, "latency_ms": round(v["latency_ms"], 1)} for k, v in by_tier.items()},
        "total_latency_ms": round(sum(s.latency_ms for s in run.steps), 1),
        "tokens_in": sum(int(s.tokens_in or 0) for s in run.steps),
        "tokens_out": sum(int(s.tokens_out or 0) for s in run.steps),
        "outputs": preview,
    }


def _load_wf(path: Path):
    from browser_platform.workflowuse.adapter import load_definition

    return load_definition(str(path))


async def run_live() -> dict:
    from browser_platform.config import Settings
    from browser_platform.models.llm import LLMConfig
    from browser_platform.models.workflow import SelectorStrategy, WorkflowStep
    from browser_platform.orchestrator.runner import WorkflowRunner
    from browser_platform.pomerado.adapter import PomeradoAdapter
    from browser_platform.store.file_store import FileStore

    key = _key()
    if not key:
        return {"skipped": True, "reason": "OPENROUTER_API_KEY is not set"}

    report: dict = {
        "started_at_ist": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "model": "qwen/qwen3.7-flash",
        "key_present": True,
        "key_length": len(key),
    }
    data_dir = ROOT / "data" / "live_smoke"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=data_dir, pomerado_host=HOST, default_model="qwen/qwen3.7-flash")
    store = FileStore(data_dir)
    llm = LLMConfig(api_key=key, model="qwen/qwen3.7-flash")
    runner = WorkflowRunner(store, settings)

    if not CHROME.exists():
        report["error"] = "google-chrome is not installed"
        return report

    proc, cdp = start_chrome(data_dir / "chrome-profile")
    report["cdp"] = cdp
    pomerado = PomeradoAdapter(HOST, root=data_dir / "integrations", cdp_url=cdp, model="qwen/qwen3.7-flash")
    try:
        # --- workflow-use Wikipedia path ---
        wiki_def = _load_wf(ROOT / "examples" / "wikipedia_search.workflow.json")
        stored = store.create_workflow(wiki_def)
        t0 = time.perf_counter()
        first = await runner.run(stored, cdp_url=cdp, variables={"term": "browser automation"}, llm=None)
        report["wikipedia_deterministic"] = {
            **summarize_run(first),
            "wall_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

        # Force a break on the "open first result" step.
        broken = store.get_workflow(stored.id)
        definition = broken.definition.model_copy(deep=True)
        click_idx = next(i for i, s in enumerate(definition.steps) if "first" in (s.description or "").lower())
        definition.steps[click_idx] = definition.steps[click_idx].model_copy(
            update={
                "cssSelector": "a.does-not-exist-forced-repair",
                "target_text": "DefinitelyMissingResult",
                "selectorStrategies": [
                    SelectorStrategy(type="css", value="a.does-not-exist-forced-repair", priority=1)
                ],
                "cachedSelector": None,
            }
        )
        # Drop any previously cached winner so the deterministic tier fails.
        broken = store.update_workflow(broken.id, definition)
        # A fresh runner so the first run's action cache cannot hide the broken selector.
        runner_repair = WorkflowRunner(store, settings)
        t1 = time.perf_counter()
        repaired = await runner_repair.run(
            broken,
            cdp_url=cdp,
            variables={"term": "browser automation"},
            llm=llm,
            write_back_repairs=True,
        )
        report["wikipedia_forced_repair"] = {
            **summarize_run(repaired),
            "wall_ms": round((time.perf_counter() - t1) * 1000, 1),
            "wrote_back": store.get_workflow(broken.id).definition.version != definition.version
            or any(s.cachedSelector for s in store.get_workflow(broken.id).definition.steps),
            "new_version": store.get_workflow(broken.id).definition.version,
        }

        # Re-run deterministically (LLM present but should not be needed if write-back worked).
        t2 = time.perf_counter()
        runner_rerun = WorkflowRunner(store, settings)
        rerun = await runner_rerun.run(
            store.get_workflow(broken.id),
            cdp_url=cdp,
            variables={"term": "browser automation"},
            llm=llm,
            write_back_repairs=True,
        )
        report["wikipedia_rerun_deterministic"] = {
            **summarize_run(rerun),
            "wall_ms": round((time.perf_counter() - t2) * 1000, 1),
            "used_llm_or_browser_use": any(s.tier in {"llm", "browser_use"} for s in rerun.steps),
        }

        # --- example.com extract + a forced click repair ---
        example_def = _load_wf(ROOT / "examples" / "example_com.workflow.json")
        # Insert a broken click before extract so repair is exercised on a public page.
        steps = list(example_def.steps)
        steps.insert(
            1,
            WorkflowStep(
                type="click",
                description="forced broken click on example.com",
                target_text="MissingExampleLink",
                selectorStrategies=[SelectorStrategy(type="css", value="a.missing-forced", priority=1)],
            ),
        )
        example_def = example_def.model_copy(update={"steps": steps, "name": "example_com_forced_repair"})
        example_stored = store.create_workflow(example_def)
        t3 = time.perf_counter()
        example_repair = await runner.run(example_stored, cdp_url=cdp, llm=llm, write_back_repairs=True)
        report["example_com_forced_repair"] = {
            **summarize_run(example_repair),
            "wall_ms": round((time.perf_counter() - t3) * 1000, 1),
        }

        # --- Pomerado mints (owned Chromium, OpenRouter) ---
        mint_timeout = float(os.environ.get("POMERADO_LIVE_WAIT_S") or 720)
        t4 = time.perf_counter()
        example_mint = await pomerado.mint(
            name="example_com_reader",
            url="https://example.com",
            intent="Read-only: open https://example.com and return the page title and a short summary of the visible text. Do not click, type, log in, or change anything.",
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
        }
    finally:
        await pomerado.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    _assert_no_key(report, key)
    return report


def main() -> int:
    report = asyncio.run(run_live())
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_key(report, _key())
    REPORT.write_text(json.dumps(report, indent=2, default=str) + "\n")
    # Print a key-free summary only.
    summary = {
        "report": str(REPORT),
        "key_present": bool(report.get("key_present") or not report.get("skipped")),
        "wikipedia_deterministic": (report.get("wikipedia_deterministic") or {}).get("status"),
        "wikipedia_forced_repair": (report.get("wikipedia_forced_repair") or {}).get("status"),
        "wikipedia_rerun_deterministic": (report.get("wikipedia_rerun_deterministic") or {}).get("status"),
        "example_com_forced_repair": (report.get("example_com_forced_repair") or {}).get("status"),
        "pomerado_mint_example_com": (report.get("pomerado_mint_example_com") or {}).get("status"),
        "pomerado_mint_wikipedia": (report.get("pomerado_mint_wikipedia") or {}).get("status"),
        "skipped": report.get("skipped"),
        "error": report.get("error"),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
