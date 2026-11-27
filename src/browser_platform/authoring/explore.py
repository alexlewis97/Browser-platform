"""Bounded post-create exploration for optional variation inputs.

After a from-task demonstration succeeds, inspect the final URL (and, cheaply,
the page) for controls the user could vary later: month/date path segments,
tabs, filters, sort, pagination, dropdowns, or query parameters. Prefer a
deterministic URL template (for example /2026-11 → {month|ym}) over a click.
Verified proposals become optional inputs whose defaults are computed at run
time (current month is not pinned).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from browser_platform.interpolate import current_year_month, interpolate_url, shift_year_month, year_month
from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput
from browser_platform.authoring.explore_controls import propose_from_html, propose_from_url_patterns
from browser_platform.workflow_inputs import pretty_label

MAX_TOKENS = 400
MAX_PROPOSALS = 6
MAX_COST_USD = 0.01
HTTP_TIMEOUT = 12
_YM = re.compile(r"(20\d{2}-\d{2})")
_BBC_TEAM_FIXTURES = re.compile(
    r"(https?://(?:www\.)?bbc\.com/sport/football/teams/[^/?#]+/scores-fixtures)(?:/(20\d{2}-\d{2}))?",
    re.I,
)


def _split_query(url: str) -> tuple[str, str]:
    if "?" in url:
        path, query = url.split("?", 1)
        return path, query
    return url, ""


def _join_query(path: str, query: str) -> str:
    return f"{path}?{query}" if query else path


def propose_from_definition(definition: WorkflowDefinition, *, task: str = "") -> list[dict[str, Any]]:
    """Deterministic proposals from navigation URLs. No LLM, no network."""

    existing = {str(item.name).lower() for item in (definition.input_schema or [])}
    already_bound = "month" in existing and any("{month" in (step.url or "") for step in definition.steps or [])
    proposals: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, step in enumerate(definition.steps or []):
        url = step.url or ""
        if not url or "://" not in url.replace("{", "http://"):
            continue
        bbc = _BBC_TEAM_FIXTURES.search(url)
        if bbc and not already_bound and "month" not in seen:
            proposals.append(
                {
                    "name": "month",
                    "label": "Month",
                    "required": False,
                    "runtime_default": "current_month",
                    "format": "YYYY-MM",
                    "description": "Optional month on the fixtures calendar. Defaults to the current month at run time.",
                    "binding": {"kind": "url_month", "step_index": index, "source_url": url},
                    "reason": "BBC fixtures URLs accept a /YYYY-MM path segment after scores-fixtures.",
                    "verify_with": shift_year_month(current_year_month(), 1),
                }
            )
            seen.add("month")
            continue
        path, _query = _split_query(url)
        if "{month" not in url and "month" not in seen and not already_bound:
            match = _YM.search(path)
            if match and re.search(r"/(20\d{2}-\d{2})(?:/|$)", path):
                proposals.append(
                    {
                        "name": "month",
                        "label": "Month",
                        "required": False,
                        "runtime_default": "current_month",
                        "format": "YYYY-MM",
                        "description": "Optional month. Defaults to the current month at run time.",
                        "binding": {
                            "kind": "url_replace",
                            "step_index": index,
                            "replace": match.group(1),
                            "placeholder": "{month|ym}",
                            "source_url": url,
                        },
                        "reason": f"Path segment {match.group(1)} looks like a month filter.",
                        "verify_with": shift_year_month(match.group(1), 1),
                    }
                )
                seen.add("month")
        proposals.extend(
            propose_from_url_patterns(url, existing=existing, step_index=index, seen=seen)
        )

    return proposals[:MAX_PROPOSALS]


def apply_proposals(definition: WorkflowDefinition, proposals: list[dict[str, Any]]) -> WorkflowDefinition:
    """Bind verified proposals onto a copy of the workflow. Month is never pinned."""

    updated = definition.model_copy(deep=True)
    names = {item.name for item in (updated.input_schema or [])}
    for proposal in proposals:
        name = proposal["name"]
        if name not in names:
            updated.input_schema = list(updated.input_schema or [])
            updated.input_schema.append(
                WorkflowInput(
                    name=name,
                    type="string",
                    required=False,
                    default=None if proposal.get("runtime_default") == "current_month" else proposal.get("default"),
                    label=proposal.get("label") or pretty_label(name),
                    description=proposal.get("description"),
                    format=proposal.get("format"),
                    runtime_default=proposal.get("runtime_default"),
                    # Explore reads the default off the live page and verifies it.
                    default_source="runtime" if proposal.get("runtime_default") else ("page" if proposal.get("default") not in (None, "") else None),
                    **({"examples": list(proposal["examples"])} if proposal.get("examples") else {}),
                    **({"enum": list(proposal["enum"])} if proposal.get("enum") else {}),
                    **({"default_view": proposal["default_view"]} if proposal.get("default_view") else {}),
                )
            )
            names.add(name)
        binding = proposal.get("binding") or {}
        index = binding.get("step_index")
        steps = list(updated.steps or [])
        if not isinstance(index, int) or index < 0 or index >= len(steps):
            # Fall back to the first navigation step.
            index = next((i for i, step in enumerate(steps) if step.url), 0)
        if not steps:
            continue
        step = steps[index]
        url = step.url or ""
        kind = binding.get("kind")
        if kind == "url_month":
            step.url = _bind_month_url(url)
        elif kind == "url_replace":
            needle = str(binding.get("replace") or "")
            placeholder = str(binding.get("placeholder") or ("{" + name + "}"))
            if needle and needle in url:
                step.url = url.replace(needle, placeholder, 1)
        elif kind == "url_segment":
            path, query = _split_query(url)
            head, sep, rest = path.partition("://")
            host, _, tail = rest.partition("/") if sep else ("", "", path.lstrip("/"))
            segs = [seg for seg in tail.split("/") if seg != ""]
            pos = binding.get("position")
            placeholder = str(binding.get("placeholder") or ("{" + name + "}"))
            if binding.get("append") and isinstance(pos, int) and pos == len(segs):
                segs.append(placeholder)
                rebuilt = (f"{head}://{host}/" if sep else "/") + "/".join(segs)
                step.url = _join_query(rebuilt, query)
            elif isinstance(pos, int) and 0 <= pos < len(segs) and "{" not in segs[pos]:
                segs[pos] = placeholder
                rebuilt = (f"{head}://{host}/" if sep else "/") + "/".join(segs)
                step.url = _join_query(rebuilt, query)
        elif kind == "url_query":
            key = str(binding.get("key") or name)
            placeholder = str(binding.get("placeholder") or ("{" + name + "}"))
            path, query = _split_query(url)
            pairs = [pair for pair in query.split("&") if pair and not pair.startswith(key + "=")]
            pairs.append(f"{key}={placeholder}")
            step.url = _join_query(path, "&".join(pairs))
        elif kind == "url_template":
            template = str(binding.get("template") or "")
            if template:
                step.url = template
        elif kind in {"select", "select_change"}:
            from browser_platform.models.workflow import WorkflowStep
            from browser_platform.authoring.elements import strategies_for_element
            element = {"css": binding.get("css"), "id": binding.get("id"), "tag": "select"}
            fill = WorkflowStep(
                type="select_change",
                description="Choose {" + name + "}",
                value="{" + name + "}",
                selectedText="{" + name + "}",
                target_text="{" + name + "}",
                cssSelector=binding.get("css"),
                selectorStrategies=strategies_for_element(element, for_fill=True) or None,
            )
            extract_at = next((i for i, item in enumerate(steps) if item.type in {"extract", "extract_page_content"}), len(steps))
            # After login/actions, immediately before extract — not after the first nav.
            steps.insert(extract_at, fill)
        elif kind in {"click", "tab", "radio"}:
            from browser_platform.models.workflow import WorkflowStep
            click = WorkflowStep(
                type="click",
                description="Open {" + name + "}",
                target_text="{" + name + "}",
                # Prefer visible label text; raw radio inputs are often not clickable.
                cssSelector=None if kind in {"radio", "tab", "click"} else binding.get("css"),
                selectorStrategies=[
                    {"type": "xpath", "value": "//label[normalize-space()='{" + name + "}']", "priority": 8},
                    {"type": "text_exact", "value": "{" + name + "}", "priority": 10},
                    {"type": "role_text", "value": "{" + name + "}", "priority": 15, "metadata": {"role": "radio"}},
                    {"type": "text_fuzzy", "value": "{" + name + "}", "priority": 40},
                    *([{"type": "css", "value": binding.get("css"), "priority": 80}] if binding.get("css") and "label[for]" != binding.get("css") else []),
                ],
            )
            extract_at = next((i for i, item in enumerate(steps) if item.type in {"extract", "extract_page_content"}), len(steps))
            steps.insert(extract_at, click)
        updated.steps = steps
    return updated


def strip_proposals(definition: WorkflowDefinition, names: list[str]) -> WorkflowDefinition:
    """Remove optional explored inputs the builder turned off."""

    drop = {str(name) for name in names}
    updated = definition.model_copy(deep=True)
    updated.input_schema = [item for item in (updated.input_schema or []) if item.name not in drop]
    for step in updated.steps or []:
        url = step.url or ""
        if not url:
            continue
        for name in drop:
            if name == "month":
                path, query = _split_query(url)
                path = re.sub(r"/\{month(?:\|[^}]+)?\}", "", path)
                step.url = _join_query(path, query)
            else:
                step.url = url.replace("{" + name + "}", "").replace("{" + name + "|ym}", "")
                url = step.url
    return updated


def _bind_month_url(url: str) -> str:
    path, query = _split_query(url)
    path = re.sub(r"/(20\d{2}-\d{2})$", "", path)
    path = re.sub(r"/\{month(?:\|[^}]+)?\}$", "", path)
    path = path.rstrip("/") + "/{month|ym}"
    return _join_query(path, query)


def sample_urls(definition: WorkflowDefinition, proposal: dict[str, Any], variables: dict[str, Any] | None = None) -> tuple[str, str]:
    """Default (runtime current) and alternate URLs for a proposal."""

    applied = apply_proposals(definition, [proposal])
    step_url = ""
    binding = proposal.get("binding") or {}
    index = binding.get("step_index")
    steps = list(applied.steps or [])
    if isinstance(index, int) and 0 <= index < len(steps):
        step_url = steps[index].url or ""
    if not step_url:
        step_url = next((step.url or "" for step in steps if step.url), "")
    base_vars = dict(variables or {})
    name = proposal.get("name")
    default = proposal.get("default")
    if name and name not in base_vars and default not in (None, "", "current"):
        base_vars[name] = default
    default_url = interpolate_url(step_url, base_vars)
    alt_value = proposal.get("verify_with")
    if not alt_value:
        kind = (proposal.get("binding") or {}).get("kind") or ""
        if name == "month" or kind == "url_month":
            alt_value = shift_year_month(current_year_month(), 1)
        else:
            return str(default_url or ""), ""
    alt_url = interpolate_url(step_url, {**base_vars, name: alt_value} if name else base_vars)
    return str(default_url or ""), str(alt_url or "")


def _fingerprint(body: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", body or "", flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()[:8000]


async def verify_proposal(
    definition: WorkflowDefinition,
    proposal: dict[str, Any],
    *,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch default and alternate URLs; keep the proposal only when the page changes."""

    default_url, alt_url = sample_urls(definition, proposal, variables)
    result = {
        **proposal,
        "default_url": default_url,
        "alt_url": alt_url,
        "verified": False,
        "dropped_reason": None,
    }
    if proposal.get("verify_mode") == "browser":
        from browser_platform.authoring.explore_verify import verify_proposal_browser

        return await verify_proposal_browser(definition, proposal, variables=variables)
    if not default_url or not alt_url or default_url == alt_url:
        result["dropped_reason"] = "default and alternate URLs did not differ"
        return result
    try:
        import httpx

        headers = {"User-Agent": "Mozilla/5.0 (compatible; BrowserPlatformExplore/1.0)", "Accept-Language": "en"}
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers) as client:
            default_res = await client.get(default_url)
            alt_res = await client.get(alt_url)
    except Exception as exc:
        result["dropped_reason"] = f"verification request failed: {type(exc).__name__}"
        return result
    result["default_status"] = default_res.status_code
    result["alt_status"] = alt_res.status_code
    if default_res.status_code >= 400 or alt_res.status_code >= 400:
        result["dropped_reason"] = f"HTTP {default_res.status_code}/{alt_res.status_code}"
        return result
    token = str(proposal.get("verify_with") or "")
    kind = (proposal.get("binding") or {}).get("kind") or ""
    if token and kind in {"url_month", "url_replace", "url_template", "url_segment", "url_query"}:
        alt_loc = str(alt_res.url)
        if token not in alt_loc and not (proposal.get("name") == "month" and year_month(token) in alt_loc):
            result["dropped_reason"] = "alternate URL did not keep the proposed value"
            return result
    if _fingerprint(default_res.text) == _fingerprint(alt_res.text):
        result["dropped_reason"] = "page content did not change for the alternate value"
        return result
    result["verified"] = True
    return result


async def explore_with_llm(
    definition: WorkflowDefinition,
    *,
    task: str,
    page_state: dict[str, Any] | None,
    llm: Any,
    existing: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Optional cheap pass when the URL did not already yield a proposal."""

    usage = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False, "model": None}
    if existing or llm is None or not page_state:
        return [], usage
    api_key = llm.key() if hasattr(llm, "key") else None
    if not api_key:
        return [], usage
    url = str((page_state or {}).get("url") or "")
    title = str((page_state or {}).get("title") or "")
    headings = " | ".join(str(h) for h in ((page_state or {}).get("headings") or [])[:8])
    text = str((page_state or {}).get("text") or "")[:1200]
    try:
        from browser_platform.llm.openrouter import chat_complete, message_text
        import json

        message, usage = await chat_complete(
            [
                {
                    "role": "system",
                    "content": (
                        "List optional variation controls on this finished page that a replay could bind "
                        "deterministically (month/date in the URL, tabs, sort, pagination, filter dropdowns). "
                        'Reply JSON only: {"proposals":[{"name":"month","label":"Month","reason":"...","binding":{"kind":"url_month"}}]}. '
                        "Prefer a URL template over a click. Do not propose required inputs or the site itself. "
                        "If there are none, return {\"proposals\":[]}."
                    ),
                },
                {"role": "user", "content": f"Task: {task}\nURL: {url}\nTitle: {title}\nHeadings: {headings}\nText: {text}"},
            ],
            max_tokens=MAX_TOKENS,
            response_format={"type": "json_object"},
            model=getattr(llm, "model", None) or "qwen/qwen3.7-flash",
            api_key=api_key,
        )
        raw = json.loads(message_text(message) or "{}")
        items = raw.get("proposals") if isinstance(raw, dict) else raw
        extra: list[dict[str, Any]] = []
        if isinstance(items, list):
            for item in items[:MAX_PROPOSALS]:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                name = re.sub(r"[^a-z0-9_]+", "_", str(item["name"]).strip().lower()).strip("_")
                if not name or name in {p["name"] for p in existing} or name in {i.name for i in definition.input_schema or []} and any(
                    "{" + name in (s.url or "") for s in definition.steps or []
                ):
                    continue
                extra.append(
                    {
                        "name": name,
                        "label": item.get("label") or pretty_label(name),
                        "required": False,
                        "runtime_default": "current_month" if name == "month" else None,
                        "description": item.get("reason") or item.get("description"),
                        "binding": item.get("binding") or {"kind": "url_month"},
                        "reason": item.get("reason") or "LLM suggestion",
                        "verify_with": shift_year_month(current_year_month(), 1) if name == "month" else item.get("verify_with"),
                    }
                )
        return extra, usage
    except Exception:
        return [], usage


async def name_option_families(
    families: list[dict[str, Any]], definition: WorkflowDefinition, *, llm: Any = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Give generic link-family options a meaningful input name (one tiny LLM call).

    Typed families (date/month/year) and query keys already have names. Without a key the
    neutral name is kept; the description still lists the page's own values."""

    usage: dict[str, Any] = {"tokens_in": 0, "tokens_out": 0, "used": False}
    todo = [f for f in families if (f.get("link_family") or {}).get("kind") == "option" and f["name"].startswith("option")]
    if not todo:
        return families, usage
    api_key = (llm.key() if llm is not None and hasattr(llm, "key") else None)
    if not api_key:
        from browser_platform.llm.openrouter import env_api_key

        api_key = env_api_key()
    if not api_key:
        return families, usage
    import json

    from browser_platform.llm.openrouter import chat_complete, message_text

    taken = {i.name for i in definition.input_schema or []} | {f["name"] for f in families}
    payload = [
        {"id": i, "values": (f.get("link_family") or {}).get("options", [])[:8],
         "labels": list(((f.get("link_family") or {}).get("labels") or {}).values())[:8]}
        for i, f in enumerate(todo)
    ]
    try:
        message, usage = await chat_complete(
            [
                {"role": "system", "content": (
                    "Each item is a set of alternative values a web page offers as links for one URL place. "
                    "Name the parameter in snake_case by what the values are (for example competition, category, "
                    "league, region, sport). Reply JSON only: {\"names\":[{\"id\":0,\"name\":\"...\",\"kind\":\"what one value is\"}]}."
                )},
                {"role": "user", "content": json.dumps({"workflow": definition.name, "items": payload}, ensure_ascii=False)[:2000]},
            ],
            max_tokens=120,
            response_format={"type": "json_object"},
            api_key=api_key,
        )
        rows = (json.loads(message_text(message) or "{}") or {}).get("names") or []
    except Exception:
        return families, usage
    for row in rows if isinstance(rows, list) else []:
        try:
            item = todo[int(row.get("id"))]
        except Exception:
            continue
        name = re.sub(r"[^a-z0-9_]+", "_", str(row.get("name") or "").lower()).strip("_")[:30]
        if not name or name in taken:
            continue
        old = item["name"]
        taken.add(name)
        item["name"] = name
        item["label"] = pretty_label(name)
        kind_text = str(row.get("kind") or "").strip()[:40]
        if kind_text:
            item["description"] = f"Optional {kind_text}, as offered by the page's own links: " + item["description"].split("e.g. ", 1)[-1]
        binding = item.get("binding") or {}
        if binding.get("placeholder"):
            binding["placeholder"] = binding["placeholder"].replace("{" + old, "{" + name, 1)
    return families, usage


async def explore_definition(
    definition: WorkflowDefinition,
    *,
    task: str = "",
    variables: dict[str, Any] | None = None,
    llm: Any = None,
    page_state: dict[str, Any] | None = None,
    enabled: bool = True,
    verify: bool = True,
) -> dict[str, Any]:
    """Detect, verify, and optionally apply cheap optional-input proposals."""

    usage = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False, "model": None}
    empty = {
        "enabled": enabled,
        "proposals": [],
        "verified": [],
        "dropped": [],
        "accepted": [],
        "usage": usage,
        "applied": False,
    }
    if not enabled:
        return empty
    proposals = propose_from_definition(definition, task=task)
    usage_extra: dict[str, Any] | None = None
    sibling_views: list[dict[str, Any]] = []
    existing = {item.name for item in (definition.input_schema or [])} | {p["name"] for p in proposals}
    html = (page_state or {}).get("html") if isinstance(page_state, dict) else None
    start_url = ""
    for step in definition.steps or []:
        if step.url:
            start_url = interpolate_url(step.url, variables)
            break
    if not html and start_url and "://" in str(start_url):
        try:
            import httpx
            headers = {"User-Agent": "Mozilla/5.0 (compatible; BrowserPlatformExplore/1.0)", "Accept-Language": "en"}
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers) as client:
                fetched = await client.get(str(start_url))
            if fetched.status_code < 400:
                html = fetched.text
        except Exception:
            html = html
    needs_render = bool(
        any(step.type in {"input", "click", "double_click", "right_click", "select_change", "key_press"} for step in (definition.steps or []))
    )
    from browser_platform.authoring.explore_verify import _looks_like_js_shell, fetch_rendered_html, snapshot_workflow
    if start_url and (needs_render or _looks_like_js_shell(html)):
        try:
            if needs_render:
                shot = await snapshot_workflow(definition, variables)
                if shot.get("ok") and shot.get("html"):
                    html = shot["html"]
                    start_url = shot.get("url") or start_url
            elif start_url:
                rendered = await fetch_rendered_html(str(start_url))
                if rendered:
                    html = rendered
        except Exception:
            pass
    if html:
        seen = {p["name"] for p in proposals}
        nav_index = next((i for i, step in enumerate(definition.steps or []) if step.url), 0)
        nav_template = (definition.steps or [None])[nav_index].url if definition.steps else ""
        from browser_platform.authoring.explore_links import propose_from_link_families

        families = propose_from_link_families(
            html, str(start_url or ""), existing=existing | seen, step_index=nav_index, step_template=nav_template or ""
        )
        from browser_platform.authoring.explore_links import propose_sibling_views

        sibling_views = propose_sibling_views(html, str(start_url or ""), nav_template or "")
        families, name_usage = await name_option_families(families, definition, llm=llm)
        usage_extra = name_usage
        html_props = propose_from_html(html, str(start_url or ""), existing=existing)
        bound_values = {str(p.get("default")) for p in proposals + html_props if p.get("default") not in (None, "")}
        # A link family on a URL place another proposal already binds is the same parameter.
        families = [
            f for f in families
            if not any(
                v and re.search(r"(?<![0-9A-Za-z])" + re.escape(v) + r"(?![0-9A-Za-z])", str((f.get("binding") or {}).get("replace") or ""))
                for v in bound_values
            )
        ]
        for item in html_props + families:
            if item["name"] not in seen:
                proposals.append(item)
                seen.add(item["name"])
    extra, llm_usage = await explore_with_llm(definition, task=task, page_state=page_state, llm=llm, existing=proposals)
    usage = {
        "tokens_in": int((llm_usage or {}).get("tokens_in") or 0) + int((usage_extra or {}).get("tokens_in") or 0),
        "tokens_out": int((llm_usage or {}).get("tokens_out") or 0) + int((usage_extra or {}).get("tokens_out") or 0),
        "cost_usd": (llm_usage or {}).get("cost_usd") or 0.0,
        "used": bool((llm_usage or {}).get("used")),
        "model": (llm_usage or {}).get("model"),
    }
    proposals = (proposals + extra)[:MAX_PROPOSALS]
    verified: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    if verify:
        browser_used = 0
        for proposal in proposals:
            if proposal.get("verify_mode") == "browser" and browser_used >= 3:
                skipped = {**proposal, "verified": False, "dropped_reason": "browser verify cap reached"}
                dropped.append(skipped)
                continue
            if proposal.get("verify_mode") == "browser":
                browser_used += 1
            checked = await verify_proposal(definition, proposal, variables=variables)
            (verified if checked.get("verified") else dropped).append(checked)
    else:
        verified = list(proposals)
    applied = apply_proposals(definition, verified) if verified else definition
    return {
        "enabled": True,
        "proposals": proposals,
        "verified": verified,
        "dropped": dropped,
        "accepted": [item["name"] for item in verified],
        "usage": usage,
        "applied": bool(verified),
        "definition": applied.model_dump(exclude_none=True) if verified else None,
        "sibling_views": sibling_views,
    }


def derive_view_definition(definition: WorkflowDefinition, view: dict[str, Any]) -> WorkflowDefinition:
    """A new tool for a sibling view: same inputs, the navigation URL switched to the view,
    a whole-page extract (the source tool's selectors belong to another page)."""

    from browser_platform.models.workflow import WorkflowStep

    derived = definition.model_copy(deep=True)
    for attr, empty in (("id", None), ("workflow_analysis", None), ("repair_history", []), ("aliases", [])):
        if hasattr(derived, attr):
            setattr(derived, attr, empty)
    label = str(view.get("label") or view.get("view") or "view").strip()
    template = str(view.get("url_template") or "")
    nav = next((step for step in derived.steps or [] if step.url), None)
    if nav is None or not template:
        raise ValueError("source tool has no navigation step or the view has no URL")
    nav.url = template
    nav.description = f"Open {template}"
    used = set(re.findall(r"\{([a-zA-Z_][\w]*)", template))
    derived.input_schema = [item for item in derived.input_schema or [] if item.name in used]
    steps = [step for step in derived.steps or [] if step.type not in {"extract", "extract_page_content"}]
    # Interactions that only made sense on the source page are dropped; navigation is kept.
    steps = [step for step in steps if step.type in {"navigation", "wait"} or step is nav]
    steps.append(WorkflowStep(type="extract", description="Extract the result", extractionGoal=label))
    derived.steps = steps
    base = re.sub(r"\s+(table|results|fixtures|scores|list)$", "", definition.name or "", flags=re.I).strip()
    derived.name = f"{base or definition.name} {label}".strip()
    derived.description = f"{label} for the same {', '.join(sorted(used)) or 'page'} as '{definition.name}' (derived from its page links)."
    return derived
