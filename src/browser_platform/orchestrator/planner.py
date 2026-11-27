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
    "You plan calls to saved browser skills (like MCP tools) to answer the user.\n"
    "Skills (name(args): summary [site]; * required, ? optional; [kind] is what an argument holds):\n{cards}\n\n"
    "Reply JSON only: {{\"intents\":[{{\"ask\":\"<one thing the user wants>\",\"steps\":[\"s1\"]}}],"
    "\"steps\":[{{\"id\":\"s1\",\"skill\":\"<name>\",\"args\":{{}},\"after\":[]}}],\"answer\":null}}\n"
    "Rules: first split the request into intents, one per entity and kind of information "
    "(for example 'X results' and 'X fixtures' are two intents; 'A and B' are two intents). "
    "Every intent needs at least one step, using the skill whose summary matches that kind of information; "
    "when two skills look alike, pick by what they return (past vs upcoming, table vs list). "
    "Use only skills listed above, with their exact argument names. "
    "Fill arguments only from values the user stated; never invent them. If a required value is missing, still add the step without it. "
    "Apply a stated value (date, month, filter) only to the intents it was said for. "
    "For :day, :month or :year arguments pass the user's own words (\"yesterday\", \"last Saturday\", \"June 2026\"); code converts them, so never compute a date yourself. "
    "Bind a value only to an argument of the same kind: a league or competition is not a club or team, and a team is not a competition. "
    "If no listed skill takes the kind of entity an intent is about, give that intent no steps rather than forcing a mismatched argument. "
    "Omit optional arguments unless the user named a specific value; never send placeholders such as current, now or default. "
    "Several values (teams, terms, dates) get one step each; steps run in parallel. A :list argument instead takes them all as one array. "
    "Use \"after\" only when a step needs an earlier step's result, and pass that value as {{\"from\":\"s1\",\"pick\":\"what to take\"}}. "
    "Never answer from your own knowledge when a listed skill can fetch it. "
    "Only if no listed skill fits, return no steps and put a short reply in \"answer\" saying what is not covered."
)

REPAIR_SYSTEM = (
    "You are a workflow tool router. Call the saved browser workflows that answer the user, one call per value, in parallel. "
    "Fill required arguments only from values the user stated; omit optional inputs unless the user named one; "
    "never send placeholders such as current, now or default."
)


def workspace_context(workspace: dict[str, Any] | None, *, limit: int = 1200) -> str:
    """Short per-workspace prompt: what system this is and any author instructions."""

    if not workspace:
        return ""
    head = f"\nWorkspace: {workspace.get('name')}"
    if workspace.get("description"):
        head += f" - {workspace['description']}"
    text = head
    if workspace.get("instructions"):
        text += f"\nWorkspace instructions: {workspace['instructions']}"
    return text[:limit]


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


def planner_messages(
    message: str,
    history: list[dict[str, Any]],
    manifests: list[dict[str, Any]],
    *,
    focus: list[str] | None = None,
    workspace: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    cards = "\n".join(f"- {card(m)}" for m in manifests)
    system = PLANNER_SYSTEM.format(cards=cards, month=current_year_month()) + workspace_context(workspace)
    user = message[:2000]
    if focus:
        user = f"Original request: {user}\nPlan steps only for these parts, which are not covered yet:\n" + "\n".join(f"- {f}" for f in focus)
    return [{"role": "system", "content": system}, *history_lines(history, message), {"role": "user", "content": user}]


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
    intents = []
    for raw in data.get("intents") or []:
        if isinstance(raw, dict) and str(raw.get("ask") or "").strip():
            ids = raw.get("steps") or []
            if isinstance(ids, str):
                ids = [ids]
            intents.append({"ask": str(raw["ask"]).strip()[:200], "steps": [str(i) for i in ids if i]})
        elif isinstance(raw, str) and raw.strip():
            intents.append({"ask": raw.strip()[:200], "steps": []})
    answer = data.get("answer")
    return {"steps": steps, "intents": intents, "answer": answer if isinstance(answer, str) and answer.strip() else None}


def uncovered_intents(intents: list[dict[str, Any]], steps: list[dict[str, Any]]) -> list[str]:
    """Intents that no surviving step serves. A step may serve an intent by id or, when the
    model left the mapping out, by being the only step for a skill nobody else claimed."""

    live = {s["id"] for s in steps}
    missing = []
    for intent in intents or []:
        if not any(i in live for i in intent.get("steps") or []):
            missing.append(intent["ask"])
    claimed = {i for intent in intents or [] for i in intent.get("steps") or []}
    spare = [s for s in steps if s["id"] not in claimed]
    # Unmapped steps can cover unmapped intents one for one (models sometimes skip the ids).
    return missing[len(spare):] if spare and len(spare) <= len(missing) else ([] if spare else missing)


_SPLIT = re.compile(r"\s*(?:,|;|\band\b|\balso\b|\bplus\b|\bthen\b|\bvs\.?\b|\bversus\b|&|\+|\n)\s*", re.I)


def sub_queries(message: str, *, limit: int = 5) -> list[str]:
    """Cheap clause split for per-intent retrieval (no LLM). Short fragments are dropped."""

    parts = [p.strip() for p in _SPLIT.split(message or "") if p and len(p.strip()) >= 3]
    return parts[:limit] if len(parts) > 1 else []


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
            elif spec.get("type") == "array":
                from browser_platform.interpolate import as_list

                value = as_list(value)
            elif spec.get("type") == "string" and isinstance(value, (list, tuple)):
                value = [str(v) for v in value if v is not None and str(v).strip()]  # fanned out per value
                if len(value) == 1:
                    value = value[0]
            elif spec.get("type") == "string" and not isinstance(value, str):
                value = str(value)
            enum = spec.get("enum")
            if enum and not isinstance(value, list) and value not in enum:
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
    workspace: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Native tool calling over a handful of full schemas."""

    messages = [{"role": "system", "content": REPAIR_SYSTEM + workspace_context(workspace)}, *history_lines(history, message), {"role": "user", "content": message[:2000]}]
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
