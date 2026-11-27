"""Build a skill manifest for a saved workflow.

A manifest is what an MCP server would publish for a tool: a slug, a one-line
summary, a longer description, a typed input schema with defaults, the output
shape, the site, tags and an example request. The deterministic part is derived
from the workflow definition; an optional, cheap LLM pass improves the prose.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlparse

from browser_platform.authoring.terminal import is_extract
from browser_platform.interpolate import is_placeholder_value
from browser_platform.models.workflow import StoredWorkflow, WorkflowDefinition
from browser_platform.workflow_inputs import (
    ensure_definition_inputs,
    default_is_trusted,
    has_runtime_default,
    input_grain,
    input_is_required,
    pretty_label,
)

MANIFEST_VERSION = 4
_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = {
    "the", "a", "an", "of", "for", "to", "and", "or", "in", "on", "with", "from", "task", "workflow",
    "explore", "debug", "recorded", "www", "com", "org", "net", "io", "http", "https", "html", "e2e",
}


def fingerprint(definition: WorkflowDefinition) -> str:
    """Stable hash of what the manifest depends on (not repairs or cache flags)."""

    definition = ensure_definition_inputs(definition.model_copy(deep=True))
    payload = {
        "name": definition.name,
        "description": definition.description,
        "inputs": [item.model_dump(exclude_none=True) for item in definition.input_schema or []],
        "steps": [
            [s.type, s.url or "", getattr(s, "extractionGoal", None) or s.goal or "", s.output or "", s.target_text or ""]
            for s in definition.steps or []
        ],
        "v": MANIFEST_VERSION,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def site_of(definition: WorkflowDefinition) -> str:
    for step in definition.steps or []:
        url = step.url or ""
        if url.startswith("http"):
            host = (urlparse(url.replace("{", "").replace("}", "")).hostname or "").lower()
            return host[4:] if host.startswith("www.") else host
    return ""


def entry_url(definition: WorkflowDefinition) -> str:
    """First navigation target as host/path with placeholders blanked, for duplicate detection."""

    for step in definition.steps or []:
        url = step.url or ""
        if url.startswith("http"):
            url = re.sub(r"\{[^}]*\}", "{}", url).split("?")[0].split("#")[0].rstrip("/")
            return re.sub(r"^https?://(www\.)?", "", url).lower()
    return ""


def structure_key(definition: WorkflowDefinition) -> str:
    """Hash of what the workflow actually does: every step's type, full URL template
    (query string included), target, selector, value template and extraction goal.

    Two workflows are duplicates only when this matches. The entry URL alone is not enough:
    '/scores-fixtures' and '/scores-fixtures?filter=results' are different skills.
    """

    rows = []
    for step in definition.steps or []:
        url = re.sub(r"\{[^}]*\}", "{}", step.url or "")
        rows.append(
            [
                (step.type or "").lower(),
                re.sub(r"^https?://(www\.)?", "", url).lower(),
                (step.target_text or "").strip().lower(),
                (step.cssSelector or "").strip(),
                re.sub(r"\{[^}]*\}", "{}", str(step.value or "")),
                (getattr(step, "extractionGoal", None) or step.goal or "").strip().lower(),
            ]
        )
    return hashlib.sha1(json.dumps(rows).encode()).hexdigest()[:16]


def words(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1]


def _input_entry(item: Any, definition: WorkflowDefinition | None = None) -> dict[str, Any]:
    runtime = has_runtime_default(item, definition)
    grain = input_grain(item, definition)
    example = item.default if item.default is not None else None
    if is_placeholder_value(example):
        example = None
    entry: dict[str, Any] = {
        "name": item.name,
        "type": item.type or "string",
        "required": bool(input_is_required(item, definition)),
        "description": item.description or pretty_label(item.name, label=item.label, description=item.description),
    }
    if grain:
        entry["grain"] = grain
    if runtime:
        entry["default"] = f"runtime:current_{grain}" if grain else ("runtime:current_month" if (item.name or "").lower() in {"month", "date"} or item.format else "runtime")
        if item.format:
            entry["format"] = item.format
    elif example is not None and str(example).strip() and default_is_trusted(item):
        entry["default" if not entry["required"] else "example"] = example
    examples = [str(v) for v in (getattr(item, "examples", None) or []) if str(v).strip() and not is_placeholder_value(v)]
    if examples:
        entry["examples"] = examples[:6]
    enum = getattr(item, "enum", None)
    if isinstance(enum, list) and enum:
        entry["enum"] = list(enum)[:20]
    default_view = getattr(item, "default_view", None)
    if default_view and not entry["required"]:
        entry["default_view"] = str(default_view)[:30]
    if item.sensitive:
        entry["sensitive"] = True
    return entry


def _output_shape(definition: WorkflowDefinition, action: bool) -> dict[str, Any]:
    goals: list[str] = []
    fields: list[str] = []
    for step in definition.steps or []:
        if is_extract(step):
            goal = getattr(step, "extractionGoal", None) or step.goal or step.description or ""
            if goal:
                goals.append(str(goal)[:160])
            fields.append(step.output or "extract")
    kind = "action" if action else ("extract" if fields else "page")
    desc = "; ".join(goals) or ("Confirmation text after the action." if action else "Page text.")
    return {"kind": kind, "fields": fields or ["extract"], "description": desc[:300]}


def base_manifest(stored: StoredWorkflow, *, slug: str, action_ending: bool, input_schema: dict[str, Any]) -> dict[str, Any]:
    definition = ensure_definition_inputs(stored.definition)
    site = site_of(definition)
    name = definition.name or slug
    desc = (definition.description or "").strip()
    inputs = [_input_entry(item, definition) for item in definition.input_schema or [] if item.name]
    goal = _output_shape(definition, action_ending)
    title = re.sub(r"[_\s]+", " ", re.sub(r"\{[^}]*\}", "", name)).strip() or slug
    summary = desc.split("\n")[0][:140] if desc else ""
    if not summary:
        verb = "Submit" if action_ending else "Get"
        summary = f"{verb} {title.lower()}" + (f" on {site}" if site else "")
    tag_words = []
    for w in words(f"{name} {site.split('.')[0] if site else ''} {goal['description']}"):
        if w not in tag_words:
            tag_words.append(w)
    req = {i["name"]: (i.get("example") or i.get("default") or i["name"]) for i in inputs if i["required"]}
    example = f"{title} " + " ".join(f"{k} {v}" for k, v in req.items()) if req else title
    return {
        "id": stored.id,
        "slug": slug,
        "name": name,
        "title": title,
        "summary": summary,
        "description": desc or f"{summary}. Returns: {goal['description']}",
        "inputs": inputs,
        "input_schema": input_schema,
        "output": goal,
        "site": site,
        "domain": site,
        "entry": entry_url(definition),
        "structure": structure_key(definition),
        "tags": tag_words[:8],
        "example_request": example.strip()[:160],
        "action_ending": bool(action_ending),
        "read_only": not action_ending,
        "fingerprint": fingerprint(definition),
        "enriched": False,
        "manifest_version": MANIFEST_VERSION,
    }


ENRICH_SYSTEM = (
    "You write tool manifests for saved browser workflows, like an MCP server describing its tools. "
    "Return JSON only: {\"summary\": one line under 90 chars starting with a verb, "
    "\"description\": 1-3 sentences on what it does, when to use it, and what it returns, "
    "\"tags\": 3-8 lowercase keywords incl. synonyms a user might type, "
    "\"example_request\": one natural user request it answers, "
    "\"output\": short description of the returned data, "
    "\"inputs\": {input_name: short description}}. Do not invent inputs. "
    "The summary must say exactly what data comes back so it cannot be confused with the sibling skills listed "
    "(for example past results vs upcoming fixtures, a table vs a list, which sport). "
    "Do not mention the siblings or the word distinct; just be specific."
)


def enrich_prompt(manifest: dict[str, Any], definition: WorkflowDefinition, siblings: list[dict[str, Any]] | None = None) -> str:
    steps = []
    for step in (definition.steps or [])[:12]:
        bits = [step.type]
        for value in (step.url, getattr(step, "extractionGoal", None), step.target_text, step.value):
            if value:
                bits.append(str(value)[:100])
        steps.append(" ".join(bits))
    return json.dumps(
        {
            "name": manifest["name"],
            "site": manifest["site"],
            "current_description": (definition.description or "")[:400],
            "inputs": [{k: v for k, v in i.items() if k in {"name", "type", "required", "default", "example"}} for i in manifest["inputs"]],
            "ends_in_action": manifest["action_ending"],
            "steps": steps,
            "sibling_skills": [
                {"name": sib.get("name"), "summary": sib.get("summary"), "entry": sib.get("entry")}
                for sib in (siblings or [])[:8]
            ],
        },
        ensure_ascii=False,
    )


def apply_enrichment(manifest: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    out = dict(manifest)
    if isinstance(data.get("summary"), str) and data["summary"].strip():
        out["summary"] = data["summary"].strip()[:120]
    if isinstance(data.get("description"), str) and data["description"].strip():
        out["description"] = data["description"].strip()[:600]
    if isinstance(data.get("tags"), list):
        tags = [str(t).strip().lower()[:30] for t in data["tags"] if str(t).strip()]
        out["tags"] = list(dict.fromkeys(tags + list(manifest.get("tags") or [])))[:12]
    if isinstance(data.get("example_request"), str) and data["example_request"].strip():
        out["example_request"] = data["example_request"].strip()[:200]
    if isinstance(data.get("output"), str) and data["output"].strip():
        out["output"] = {**(manifest.get("output") or {}), "description": data["output"].strip()[:300]}
    descs = data.get("inputs") if isinstance(data.get("inputs"), dict) else {}
    if descs:
        inputs = []
        for item in manifest.get("inputs") or []:
            new = dict(item)
            if isinstance(descs.get(item["name"]), str) and descs[item["name"]].strip():
                new["description"] = descs[item["name"]].strip()[:160]
            inputs.append(new)
        out["inputs"] = inputs
    out["enriched"] = True
    out["enriched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return out


def parse_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def input_kind(item: dict[str, Any], *, words_max: int = 4) -> str:
    """Short entity-kind hint from an input's description ("Club name", "competition slug"),
    so the planner can tell a team input from a competition input."""

    text = str(item.get("description") or "").strip()
    if not text:
        return ""
    head = re.split(r"[,.;:(]| such as | e\.g\.| for example", text, maxsplit=1)[0].strip()
    toks = head.split()
    if not toks or len(toks) > words_max + 2:
        toks = toks[:words_max]
    hint = " ".join(toks[:words_max]).lower()
    if hint in {"", str(item.get("name") or "").lower().replace("_", " ")}:
        return ""
    return hint


def card(manifest: dict[str, Any]) -> str:
    """One compact line per skill for the planner (~20-40 tokens)."""

    args = []
    for item in manifest.get("inputs") or []:
        if item.get("sensitive"):
            continue
        mark = "*" if item.get("required") else "?"
        extra = ""
        if item.get("enum"):
            extra = "=" + "|".join(str(v) for v in item["enum"][:6])
            if item.get("default_view"):
                # What the page shows when the argument is left out (e.g. a "Today" view).
                extra += f" (omit={item['default_view']})"
        elif item.get("grain"):
            # Typed dates: the planner passes the user's words; code resolves them.
            extra = ":" + {"date": "day", "month": "month", "year": "year"}[item["grain"]]
        elif str(item.get("default") or "").startswith("runtime"):
            extra = "=YYYY-MM" if (item.get("name") or "").lower() in {"month", "date"} else "=auto"
        elif item.get("type") and item["type"] != "string":
            extra = f":{item['type']}"
        else:
            kind = input_kind(item)
            if kind:
                extra = f"[{kind}]"
        args.append(f"{item['name']}{mark}{extra}")
    site = f" [{manifest['site']}]" if manifest.get("site") else ""
    act = " (action)" if manifest.get("action_ending") else ""
    return f"{manifest['slug']}({', '.join(args)}): {manifest.get('summary') or manifest.get('title')}{site}{act}"


def search_text(manifest: dict[str, Any]) -> str:
    inputs = " ".join(f"{i.get('name')} {i.get('description') or ''}" for i in manifest.get("inputs") or [])
    return " ".join(
        str(x or "")
        for x in (
            manifest.get("slug"),
            manifest.get("title"),
            manifest.get("summary"),
            manifest.get("description"),
            " ".join(manifest.get("tags") or []),
            manifest.get("site"),
            (manifest.get("site") or "").split(".")[0],
            inputs,
            manifest.get("example_request"),
            (manifest.get("output") or {}).get("description"),
            " ".join(manifest.get("aliases") or []),
        )
    )
