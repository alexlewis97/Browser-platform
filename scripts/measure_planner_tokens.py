"""Compare planner prompt tokens: old full-catalog tool calling vs skill retrieval + cards.

Runs only the selection step (no browser), against a data dir (default: ./data).
Uses OPENROUTER_API_KEY / OPENAI_API_KEY from the environment; never prints them.

  python scripts/measure_planner_tokens.py --data /path/to/data [--backfill]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from browser_platform.authoring.tools import openai_tools  # noqa: E402
from browser_platform.llm.openrouter import chat_complete, message_text  # noqa: E402
from browser_platform.orchestrator.chat import _parse_tool_calls  # noqa: E402
from browser_platform.orchestrator.planner import TOP_K, parse_plan, planner_messages, retrieval_query  # noqa: E402
from browser_platform.skills.registry import SkillRegistry  # noqa: E402
from browser_platform.skills.retrieve import rank_skills  # noqa: E402
from browser_platform.store.file_store import FileStore  # noqa: E402

CASES = {
    "single": "What are Arsenal's upcoming football fixtures? Use the football fixtures workflow with team Arsenal.",
    "fanout_compare": "Which of Arsenal, Chelsea and Spurs plays first? Give a direct answer and a compact combined table.",
    "month_chat": "What are Arsenal's football fixtures in December 2026? Use the football fixtures workflow with team Arsenal and month 2026-12.",
    "saucedemo_chat": "On SauceDemo, add Sauce Labs Bike Light to the cart and check out as Sam Taylor, zip 10001.",
    "demoqa_chat": "Submit the DemoQA practice form for Sam Taylor, email sam@example.com, gender Female, mobile 9123456789.",
    "httpbin_chat": "Submit the httpbin pizza form for Jane Roe, size medium, toppings mushroom.",
}

OLD_SYSTEM = (
    "You are a workflow tool router. Call one or more saved browser workflows to answer the user. "
    "Fill required arguments from values the user already stated (teams, names, terms, dates). Only ask a short question when a required input is not present in the user's message. Never invent values the user did not state. "
    "Optional inputs such as month must be omitted unless the user named a specific value. Never send placeholders such as current, now, default, or auto. "
    "When the same workflow should run for several values (a list of teams, terms, dates), emit one function call per value, in parallel. "
    "Do not pack several values into one argument. Prefer the workflow that matches the request. "
    "Do not answer from memory when a tool can fetch the live result."
)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "data" / "planner_tokens_report.json"))
    args = ap.parse_args()
    store = FileStore(Path(args.data))
    reg = SkillRegistry(store)
    report: dict = {"cases": {}}
    if args.backfill:
        report["backfill"] = await reg.backfill()
    tools, manifests = reg.catalog()
    pool = reg.selection_pool(manifests)
    report["library"] = {"workflows": len(tools), "distinct_skills": len(pool), "full_schema_chars": len(json.dumps(openai_tools(tools)))}
    for name, msg in CASES.items():
        old_lines = [f"- {t['function']['name']} (workflow {t.get('workflow_name')}; required: {', '.join(t['function']['parameters'].get('required') or []) or 'none'})" for t in tools]
        _old_msg, old_usage = await chat_complete(
            [{"role": "system", "content": OLD_SYSTEM}, {"role": "user", "content": msg}], tools=openai_tools(tools), max_tokens=800
        )
        old_calls = [c["name"] for c in _parse_tool_calls(_old_msg, tools)]
        ranked = await rank_skills(retrieval_query(msg, []), pool, k=TOP_K, embedder=reg.embedder)
        new_msg, new_usage = await chat_complete(planner_messages(msg, [], ranked["skills"]), max_tokens=600, response_format={"type": "json_object"})
        plan = parse_plan(message_text(new_msg)) or {}
        report["cases"][name] = {
            "before_plan_tokens_in": old_usage["tokens_in"],
            "after_plan_tokens_in": new_usage["tokens_in"],
            "embed_tokens": ranked["embed_tokens"],
            "method": ranked["method"],
            "candidates": [m["slug"] for m in ranked["skills"]],
            "before_calls": old_calls,
            "after_steps": [(s["skill"], s["args"]) for s in plan.get("steps") or []],
            "unused_old_lines": len(old_lines),
        }
        print(name, old_usage["tokens_in"], "->", new_usage["tokens_in"], ranked["method"], [s["skill"] for s in plan.get("steps") or []], flush=True)
    Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
