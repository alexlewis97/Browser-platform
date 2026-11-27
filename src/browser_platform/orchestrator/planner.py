"""Plan-and-execute skill planner.

Selection is two-tier so the prompt never carries the whole library:

1. Retrieve the top-k skills for the request (BM25 + cached embeddings) and show the
   planner one compact card per skill (slug, args, one line).
2. The planner returns a structured plan: steps with arguments, optional ``after``
   dependencies, and ``{"from": "s1", "pick": "..."}`` references for values that only an
   earlier result can supply. Independent steps fan out in parallel.
3. Arguments are checked against the full schema of only the chosen skills. When something
   does not fit (unknown skill, unknown argument, bad enum) a repair pass runs native tool
   calling with just those full schemas.
"""

from __future__ import annotations

import json
import re
from typing import Any

from browser_platform.authoring.tools import lookup_tool, openai_tools
from browser_platform.interpolate import current_year_month
from browser_platform.llm.openrouter import chat_complete, merge_usage, message_text
from browser_platform.skills.manifest import card, parse_json_object

TOP_K = 6

PLANNER_SYSTEM = (
    "You plan calls to saved browser skills (like MCP tools) to answer the user. Today is {month}.\n"
    "Skills (name(args): summary [site]; * required, ? optional):\n{cards}\n\n"
    "Reply JSON only: {{\"steps\":[{{\"id\":\"s1\",\"skill\":\"<name>\",\"args\":{{}},\"after\":[]}}],\"answer\":null}}\n"
    "Rules: use only skills listed above, with their exact argument names. "
    "Fill arguments only from values the user stated; never invent them. If a required value is missing, still add the step without it. "
    "Omit optional arguments unless the user named a specific value; never send placeholders such as current, now or default. "
    "For several values (teams, terms, dates) add one step per value; they run in parallel. "
    "Use \"after\" only when a step needs an earlier step's result, and pass that value as {{\"from\":\"s1\",\"pick\":\"what to take\"}}. "
    "If no skill fits, return no steps and put a short reply or question in \"answer\"."
)

REPAIR_SYSTEM = (
    "You are a workflow tool router. Call the saved browser workflows that answer the user, one call per value, in parallel. "
    "Fill required arguments only from values the user stated; omit optional inputs unless the user named one; "
    "never send placeholders such as current, now or default."
)


def history_lines(history: list[dict[str, Any]], message: str, *, turns: int = 4, chars: int = 300) -> list[dict[str, str]]:
    out = []
    for turn in (history or [])[-turns:]:
        role = turn.get("role")
        content = str(turn.get("content") or "").strip()
        if role in {"user", "assistant"} and content and content != message.strip():
            out.append({"role": role, "content": content[:chars]})
    return out


def retrieval_query(message: str, history: list[dict[str, Any]]) -> str:
    """The request plus the previous user turn, so follow-ups ("and Chelsea?") still retrieve."""

    prev = [str(t.get("content") or "") for t in (history or []) if t.get("role") == "user" and str(t.get("content") or "").strip() != message.strip()]
    extra = prev[-1][:300] if prev and len(message) < 80 else ""
    return f"{message} {extra}".strip()


def planner_messages(message: str, history: list[dict[str, Any]], manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards = "\n".join(f"- {card(m)}" for m in manifests)
    system = PLANNER_SYSTEM.format(cards=cards, month=current_year_month())
    return [{"role": "system", "content": system}, *history_lines(history, message), {"role": "user", "content": message[:2000]}]


def parse_plan(text: str) -> dict[str, Any] | None:
    data = parse_json_object(text)
    if not data or not isinstance(data.get("steps", []), list):
        return None
    steps = []
    for index, raw in enumerate(data.get("steps") or [], 1):
        if not isinstance(raw, dict):
            continue
        skill = str(raw.get("skill") or raw.get("tool") or raw.get("name") or "").strip()
        if not skill:
            continue
        args = raw.get("args") if isinstance(raw.get("args"), dict) else raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
        after = raw.get("after") or raw.get("depends_on") or []
        if isinstance(after, str):
            after = [after]
        steps.append({"id": str(raw.get("id") or f"s{index}"), "skill": skill, "args": dict(args), "after": [str(a) for a in after if a]})
    answer = data.get("answer")
    return {"steps": steps, "answer": answer if isinstance(answer, str) and answer.strip() else None}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def is_ref(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("from"))


def check_step(step: dict[str, Any], tools: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    """Bind a plan step to a tool and coerce arguments to its full schema. Returns (tool, problems)."""

    tool = lookup_tool(tools, step["skill"])
    if tool is None:
        wanted = _norm(step["skill"])
        tool = next((t for t in tools if _norm((t.get("function") or {}).get("name", "")) == wanted), None)
        if tool is not None:
            step["skill"] = tool["function"]["name"]
    if tool is None:
        return None, [f"unknown skill {step['skill']}"]
    params = (tool["function"].get("parameters") or {}).get("properties") or {}
    by_norm = {_norm(k): k for k in params}
    problems: list[str] = []
    fixed: dict[str, Any] = {}
    for key, value in (step.get("args") or {}).items():
        name = key if key in params else by_norm.get(_norm(key))
        if name is None:
            problems.append(f"unknown argument {key}")
            continue
        spec = params[name]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if not is_ref(value):
            if spec.get("type") == "number" and isinstance(value, str):
                try:
                    value = float(value) if "." in value else int(value)
                except ValueError:
                    problems.append(f"{name} should be a number")
            elif spec.get("type") == "string" and not isinstance(value, str):
                value = str(value)
            enum = spec.get("enum")
            if enum and value not in enum:
                match = next((e for e in enum if str(e).lower() == str(value).lower()), None)
                if match is None:
                    problems.append(f"{name} must be one of {enum}")
                else:
                    value = match
        fixed[name] = value
    step["args"] = fixed
    return tool, problems


async def repair_calls(
    message: str,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    api_key: str,
    model: str | None,
    parse_tool_calls,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Native tool calling over a handful of full schemas."""

    messages = [{"role": "system", "content": REPAIR_SYSTEM}, *history_lines(history, message), {"role": "user", "content": message[:2000]}]
    assistant, usage = await chat_complete(messages, tools=openai_tools(tools), max_tokens=600, api_key=api_key, model=model)
    calls = parse_tool_calls(assistant, tools)
    return [{"id": f"s{i}", "skill": c["name"], "args": c.get("arguments") or {}, "after": []} for i, c in enumerate(calls, 1)], usage, message_text(assistant)


async def resolve_refs(call: dict[str, Any], items_by_id: dict[str, dict[str, Any]], *, api_key: str | None, model: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Fill {"from": id, "pick": ...} arguments from earlier results with one tiny LLM call."""

    refs = {k: v for k, v in (call.get("arguments") or {}).items() if is_ref(v)}
    if not refs:
        return call.get("arguments") or {}, None
    context = {}
    for name, ref in refs.items():
        src = items_by_id.get(str(ref.get("from")))
        context[name] = {"pick": ref.get("pick") or name, "from_result": ((src or {}).get("facts") or "")[:1200]}
    message, usage = await chat_complete(
        [
            {"role": "system", "content": "Extract each requested value from the earlier result. Reply JSON only: {\"values\": {name: value}}. Use null when it is not there."},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        max_tokens=150,
        response_format={"type": "json_object"},
        api_key=api_key,
        model=model,
    )
    values = (parse_json_object(message_text(message)).get("values") or {})
    out = dict(call.get("arguments") or {})
    for name in refs:
        value = values.get(name) if isinstance(values, dict) else None
        if value in (None, ""):
            out.pop(name, None)
        else:
            out[name] = str(value)
    return out, usage


def waves(calls: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Topological layers by ``after``; a cycle or dangling id falls back to running in order."""

    ids = {c["id"] for c in calls}
    done: set[str] = set()
    pending = list(calls)
    layers: list[list[dict[str, Any]]] = []
    while pending:
        layer = [c for c in pending if all(a in done or a not in ids for a in c.get("after") or [])]
        if not layer:
            layer = [pending[0]]
        layers.append(layer)
        done |= {c["id"] for c in layer}
        pending = [c for c in pending if c["id"] not in done]
    return layers


def sum_usage(*parts: dict[str, Any] | None) -> dict[str, Any]:
    return merge_usage(*parts)
