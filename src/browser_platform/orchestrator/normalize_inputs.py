"""Normalize chat tool inputs to the canonical entity a workflow expects."""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Iterable

from browser_platform.executor.output_validate import is_nav_footer_only
from browser_platform.interpolate import path_slug
from browser_platform.llm.openrouter import chat_complete, env_api_key, merge_usage, message_text
from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput

CompleteFn = Callable[..., Awaitable[tuple[dict[str, Any], dict[str, Any]]]]

_PLACEHOLDER = re.compile(r"\{+\s*([a-zA-Z_][\w]*)(?:\|([a-zA-Z0-9_]+))?\s*\}+")

# Form / control fields that must stay as the user typed them.
_SKIP_NAMES = {
    "first_name", "last_name", "given_name", "family_name", "middle_name", "full_name",
    "email", "e_mail", "phone", "telephone", "mobile", "password", "passcode", "otp",
    "zip", "zip_code", "postal", "postal_code", "address", "street", "card", "card_number",
    "cvv", "cvc", "ssn", "quantity", "qty", "count", "age", "dob", "date_of_birth",
    "message", "comment", "notes", "text", "body", "password_confirm",
    "month", "year", "date", "page", "category", "sort", "order_by", "product_sort",
    "size", "topping", "toppings", "gender", "colour", "color", "product", "item",
    "query", "q", "term", "search", "keyword",
}

# Named entities whose nicknames need to become the official site value / slug.
_ENTITY_NAMES = {
    "team", "club", "side", "opponent", "opposition", "franchise",
    "organisation", "organization", "org", "university", "college", "school",
    "company", "vendor", "artist", "author", "player", "athlete",
    "venue", "stadium", "ground", "competition", "league", "tournament",
    "country", "nation", "city", "region", "party", "candidate",
    "institution", "department", "agency", "publisher", "museum", "hospital",
    "airline", "airport", "station", "constituency", "club_name", "team_name",
}

def _schema_item(definition: WorkflowDefinition | None, name: str) -> WorkflowInput | None:
    if definition is None:
        return None
    lowered = (name or "").lower()
    for item in definition.input_schema or []:
        if (item.name or "").lower() == lowered:
            return item
    return None


def _examples(item: WorkflowInput | None) -> list[str]:
    if item is None:
        return []
    out: list[str] = []
    for raw in (
        item.default,
        getattr(item, "example", None),
        *list(getattr(item, "examples", None) or []),
        *list(getattr(item, "enum", None) or []),
    ):
        text = str(raw).strip() if raw is not None else ""
        if text and text not in out:
            out.append(text)
    if getattr(item, "model_dump", None):
        data = item.model_dump()
        for key in ("example", "examples", "enum"):
            raw = data.get(key)
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                text = str(value).strip() if value is not None else ""
                if text and text not in out:
                    out.append(text)
    return out


def _step_blobs(definition: WorkflowDefinition | None) -> Iterable[str]:
    if definition is None:
        return []
    blobs: list[str] = []
    for step in definition.steps or []:
        parts = [
            getattr(step, "url", None) or "",
            getattr(step, "cssSelector", None) or "",
            getattr(step, "value", None) or "",
            getattr(step, "selectedText", None) or "",
            getattr(step, "xpath", None) or "",
        ]
        for strategy in getattr(step, "selectorStrategies", None) or []:
            parts.append(getattr(strategy, "value", None) or "")
        blobs.append(" ".join(str(p) for p in parts if p))
    return blobs


def used_in_url_path(definition: WorkflowDefinition | None, name: str) -> bool:
    lowered = (name or "").lower()
    for step in getattr(definition, "steps", None) or []:
        typ = (getattr(step, "type", None) or "").lower()
        url = getattr(step, "url", None) or ""
        if typ not in {"navigation", "navigate"} and "{" + lowered not in url.lower():
            continue
        if not url:
            continue
        for match in _PLACEHOLDER.finditer(url):
            if match.group(1).lower() == lowered:
                return True
    return False


def is_entity_parameter(name: str, definition: WorkflowDefinition | None = None) -> bool:
    key = re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_")
    if not key or key in _SKIP_NAMES or key.endswith(("_email", "_phone", "_zip", "_password")):
        return False
    if key in _ENTITY_NAMES or key.endswith(("_team", "_club", "_org")):
        return True
    item = _schema_item(definition, name)
    blob = " ".join(
        [
            (getattr(item, "description", None) or ""),
            (getattr(item, "label", None) or ""),
            (getattr(definition, "description", None) or ""),
        ]
    ).lower()
    if any(word in blob for word in ("team", "club", "organisation", "organization", "university", "league")):
        return True
    # A path placeholder (especially {name|slug}) is an entity, but not month/page (skipped above).
    return used_in_url_path(definition, name)


def match_canonical_example(value: str, examples: Iterable[str]) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    slug = path_slug(text)
    compact = re.sub(r"[^a-z0-9]+", "", text.lower())
    for example in examples:
        candidate = str(example or "").strip()
        if not candidate:
            continue
        if candidate.lower() == text.lower():
            return candidate
        if path_slug(candidate) == slug:
            return candidate
        if re.sub(r"[^a-z0-9]+", "", candidate.lower()) == compact:
            return candidate
    return None


def _prefers_slug_form(examples: list[str]) -> bool:
    named = [ex for ex in examples if ex]
    if not named:
        return False
    return all(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", ex) or ex == path_slug(ex) for ex in named) and any("-" in ex for ex in named)


def _url_templates(definition: WorkflowDefinition | None, name: str) -> list[str]:
    lowered = (name or "").lower()
    found: list[str] = []
    for step in getattr(definition, "steps", None) or []:
        url = getattr(step, "url", None) or ""
        if not url:
            continue
        if any(m.group(1).lower() == lowered for m in _PLACEHOLDER.finditer(url)):
            found.append(url)
    return found


def _choose_form(canonical: str, slug: str | None, examples: list[str]) -> str:
    official = (canonical or "").strip()
    slug_val = (slug or path_slug(official)).strip()
    if not official:
        return official
    if _prefers_slug_form(examples) and slug_val:
        return slug_val
    return official


def parameter_hints(definition: WorkflowDefinition | None, name: str) -> dict[str, Any]:
    item = _schema_item(definition, name)
    examples = _examples(item)
    return {
        "name": name,
        "label": getattr(item, "label", None) or name,
        "description": getattr(item, "description", None) or "",
        "examples": examples,
        "urls": _url_templates(definition, name),
    }


async def llm_normalize_values(
    items: list[dict[str, Any]],
    *,
    complete: CompleteFn | None = None,
) -> tuple[dict[tuple[str, str], dict[str, str]], dict[str, Any]]:
    """Map (call_id, param) -> {canonical, slug}. Empty when unused or on failure."""

    usage = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False}
    pending = [item for item in items if str(item.get("value") or "").strip()]
    if not pending:
        return {}, usage
    user_words = ""
    if any(item.get("check") for item in pending):
        user_words = str(pending[0].get("_user_words") or "")[:1200]
    if complete is None and not env_api_key():
        return {}, usage
    payload = []
    for item in pending:
        payload.append(
            {
                "call": item.get("call"),
                "param": item.get("name"),
                "value": item.get("value"),
                "description": item.get("description") or item.get("label") or "",
                "examples": item.get("examples") or [],
                "urls": item.get("urls") or [],
                **({"check": True} if item.get("check") else {}),
            }
        )
    system = (
        "You normalize entity inputs to the official name a website workflow expects. "
        "Use the parameter description, examples, and URL pattern (including {name|slug}). "
        "Map nicknames and abbreviations to that official name and its URL slug "
        "(for example Spurs → Tottenham Hotspur / tottenham-hotspur, "
        "Man Utd → Manchester United / manchester-united). "
        "Leave a value unchanged when it is already official or you are not sure it is the same entity. "
        "Do not invent a different entity. "
        "If the value is clearly a different kind of thing than the parameter describes "
        "(for example a league or competition given for a club parameter, or a person for a place), "
        'add "fits":false and "kind":"<what the value is>". '
        'For items with "check":true, also add "from": the exact words copied from user_words that name or '
        "refer to that value (a nickname, abbreviation or translation counts, e.g. \"Spurs\"), or \"\" when "
        "nothing in user_words refers to it (a value picked as an example, or to stand in for a whole league "
        "or group). "
        'Reply JSON only: '
        '{"values":[{"call":"c1","param":"team","canonical":"Official Name","slug":"official-name","fits":true}]}.'
    )
    fn = complete or chat_complete
    try:
        message, usage = await fn(
            [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"inputs": payload, **({"user_words": user_words} if user_words else {})}, ensure_ascii=False
                    )[:4200],
                },
            ],
            max_tokens=300,
            response_format={"type": "json_object"},
        )
    except Exception:
        return {}, usage
    text = message_text(message) if isinstance(message, dict) else str(message or "")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        data = json.loads(text or "{}")
    except Exception:
        return {}, usage or {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False}
    rows = data.get("values") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = [data] if isinstance(data, dict) and data.get("canonical") else []
    checked = {(str(item.get("call") or ""), str(item.get("name") or "")) for item in pending if item.get("check")}
    mapped: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        call_id = str(row.get("call") or "")
        param = str(row.get("param") or row.get("name") or "")
        canonical = str(row.get("canonical") or row.get("value") or "").strip()
        slug = str(row.get("slug") or "").strip()
        if not param:
            continue
        if row.get("fits") is False:
            mapped[(call_id, param)] = {"canonical": "", "slug": "", "fits": False, "kind": str(row.get("kind") or "").strip()[:60]}
            continue
        if (call_id, param) in checked and _not_from_user(row, user_words):
            mapped[(call_id, param)] = {"canonical": "", "slug": "", "stated": False}
            continue
        if not canonical:
            continue
        mapped[(call_id, param)] = {"canonical": canonical, "slug": slug or path_slug(canonical)}
    return mapped, usage or {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False}


def apply_local_canonical(value: str, hints: dict[str, Any]) -> str | None:
    return match_canonical_example(value, hints.get("examples") or [])


def _not_from_user(row: dict[str, Any], user_words: str) -> bool:
    """True only when the model gave no span, or a span that is not in the user's words.

    The span is verified by code, so a model cannot vouch for a value the user never wrote."""

    if "from" not in row:
        return row.get("stated") is False
    span = " ".join(str(row.get("from") or "").lower().split())
    if not span:
        return True
    return span not in " ".join((user_words or "").lower().split())


_GENERIC_TOKENS = {"the", "and", "of", "fc", "cf", "afc", "club", "team", "city", "united", "town", "sport", "league", "cup"}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", (text or "").lower())


def stated_in(value: str, context: str) -> bool:
    """Cheap check that an entity value comes from the user's words (or an obvious short form)."""

    if not context:
        return True
    ctx = " ".join(_tokens(context))
    val_tokens = _tokens(value)
    if not val_tokens:
        return True
    if " ".join(val_tokens) in ctx:
        return True
    ctx_tokens = set(ctx.split())
    if any(len(t) >= 3 and t not in _GENERIC_TOKENS and t in ctx_tokens for t in val_tokens):
        return True
    initials = "".join(t[0] for t in val_tokens if t not in {"the", "of", "and"})
    if len(initials) >= 2 and initials in ctx_tokens:
        return True
    return False


async def normalize_prepared_calls(
    calls: list[dict[str, Any]],
    *,
    definitions: dict[str, WorkflowDefinition] | None = None,
    complete: CompleteFn | None = None,
    user_words: str | None = None,
) -> dict[str, Any]:
    """Rewrite entity arguments in place. Returns LLM usage.

    With ``user_words`` (the request plus recent turns), an entity value that the user never
    named is double-checked in the same normaliser call; if the model confirms it was not
    stated (the planner picked an example club for a league question, say), the call gets
    ``unstated`` and is not run with an invented value."""

    pending: list[dict[str, Any]] = []
    for call in calls:
        raw = dict(call.get("raw_arguments") or call.get("arguments") or {})
        call["raw_arguments"] = dict(raw)
        args = dict(raw)
        definition = (definitions or {}).get(call.get("workflow_id") or "")
        if definition is None:
            definition = call.get("_definition")
        call["arguments"] = args
        for name, value in list(args.items()):
            text = "" if value is None else str(value).strip()
            if not text or not is_entity_parameter(name, definition):
                continue
            hints = parameter_hints(definition, name)
            check = bool(user_words) and not stated_in(text, user_words or "")
            local = apply_local_canonical(text, hints)
            if local and local != text:
                args[name] = _choose_form(local, path_slug(local), hints.get("examples") or [])
                if check:
                    pending.append({"call": call.get("id"), "name": name, "value": text, **hints, "check": True,
                                    "check_only": True, "_user_words": user_words, "workflow_id": call.get("workflow_id")})
                continue
            if local == text or (local and path_slug(local) == path_slug(text) and local.lower() == text.lower()):
                # Already matches an example spelling, aside from case — keep official example.
                if local and local != text:
                    args[name] = local
                if check:
                    pending.append({"call": call.get("id"), "name": name, "value": text, **hints, "check": True,
                                    "check_only": True, "_user_words": user_words, "workflow_id": call.get("workflow_id")})
                continue
            pending.append(
                {
                    "call": call.get("id"),
                    "name": name,
                    "value": text,
                    **hints,
                    **({"check": True, "_user_words": user_words} if check else {}),
                    "workflow_id": call.get("workflow_id"),
                }
            )
        call["arguments"] = args
        call["normalized"] = True
    mapped, usage = await llm_normalize_values(pending, complete=complete)
    for call in calls:
        definition = (definitions or {}).get(call.get("workflow_id") or "") or call.get("_definition")
        args = dict(call.get("arguments") or {})
        raw = call.get("raw_arguments") or {}
        for name, value in list(args.items()):
            text = "" if value is None else str(value).strip()
            row = mapped.get((str(call.get("id") or ""), name))
            if not row:
                continue
            if row.get("stated") is False:
                hints = parameter_hints(definition, name)
                call.setdefault("unstated", []).append(
                    {"param": name, "value": text, "expects": hints.get("description") or hints.get("label") or name}
                )
                continue
            if row.get("fits") is False:
                hints = parameter_hints(definition, name)
                call.setdefault("kind_mismatch", []).append(
                    {"param": name, "value": text, "value_kind": row.get("kind") or "", "expects": hints.get("description") or hints.get("label") or name}
                )
                continue
            # If the first pass already changed this field locally, do not overwrite.
            if raw.get(name) is not None and str(raw.get(name)).strip() != text:
                continue
            hints = parameter_hints(definition, name)
            chosen = _choose_form(row.get("canonical") or "", row.get("slug"), hints.get("examples") or [])
            if chosen:
                args[name] = chosen
        call["arguments"] = args
    return usage


async def normalize_arguments(
    definition: WorkflowDefinition | None,
    arguments: dict[str, Any],
    *,
    call_id: str = "c1",
    complete: CompleteFn | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    call = {"id": call_id, "arguments": dict(arguments or {}), "workflow_id": "wf"}
    usage = await normalize_prepared_calls([call], definitions={"wf": definition} if definition is not None else None, complete=complete)
    return dict(call.get("arguments") or {}), usage
