"""Trim a recorded workflow to the causal path of the final extract or action."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from browser_platform.authoring.terminal import ensure_terminal, is_action, last_action, last_terminal, protect_last_action
from browser_platform.models.workflow import WorkflowStep

_PLACEHOLDER = re.compile(r"\{[a-zA-Z_][\w]*(?:\|[a-zA-Z0-9_]+)?\}")
_CHROME = re.compile(
    r"(dropdown-checkbox|hamburger|main-menu|navtoggle|vector-main-menu|sidebar-toggle|cookie|consent)",
    re.I,
)
_FLASH_PRICES = {"input": 0.03, "output": 0.13}  # USD / 1M, OpenRouter 24 Nov 2026


def estimate_cost_usd(tokens_in: int, tokens_out: int, observed: float | None = None, model: str | None = None) -> float | None:
    if observed is not None:
        try:
            value = float(observed)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    if not tokens_in and not tokens_out:
        return 0.0 if observed == 0 else None
    prices = _FLASH_PRICES
    return round((int(tokens_in or 0) / 1_000_000) * prices["input"] + (int(tokens_out or 0) / 1_000_000) * prices["output"], 8)


def usage_from_history(history: Any) -> tuple[int, int, float | None]:
    """Read browser-use AgentHistoryList (or a similar object) without dropping cost."""

    if history is None:
        return 0, 0, None
    usage = getattr(history, "usage", None)
    raw: dict[str, Any] = {}
    if isinstance(usage, dict):
        raw = dict(usage)
    elif usage is not None:
        try:
            raw = dict(usage)
        except Exception:
            raw = {
                "input": getattr(usage, "input", None) or getattr(usage, "input_tokens", 0),
                "output": getattr(usage, "output", None) or getattr(usage, "output_tokens", 0),
            }
    tokens_in = int(raw.get("input") or raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
    tokens_out = int(raw.get("output") or raw.get("completion_tokens") or raw.get("output_tokens") or 0)
    cost = getattr(history, "cost_usd", None)
    if cost is None and raw.get("cost_usd") is not None:
        cost = raw.get("cost_usd")
    if not tokens_in and not tokens_out:
        tokens_in, tokens_out, cost = _usage_from_messages(getattr(history, "messages", None), cost)
    return tokens_in, tokens_out, estimate_cost_usd(tokens_in, tokens_out, cost)


def _usage_from_messages(messages: Any, cost: float | None) -> tuple[int, int, float | None]:
    if not messages:
        return 0, 0, cost
    inn = out = 0
    for message in messages:
        usage = getattr(message, "usage", None)
        if usage is None and isinstance(message, dict):
            usage = message.get("usage")
        if usage is None:
            continue
        inn += int(getattr(usage, "input_tokens", None) or getattr(usage, "input", None) or (usage.get("input_tokens") if isinstance(usage, dict) else 0) or 0)
        out += int(getattr(usage, "output_tokens", None) or getattr(usage, "output", None) or (usage.get("output_tokens") if isinstance(usage, dict) else 0) or 0)
    return inn, out, cost


def _blob(step: WorkflowStep) -> str:
    bits = [
        step.type,
        step.description or "",
        step.target_text or "",
        step.cssSelector or "",
        step.xpath or "",
        step.value or "",
        step.url or "",
    ]
    for strategy in step.selectorStrategies or []:
        bits.append(strategy.value or "")
        bits.append(str(strategy.metadata or ""))
    return " ".join(bits)


def _is_extract(step: WorkflowStep) -> bool:
    return step.type in {"extract", "extract_page_content"}


def _embeds_variable(url: str | None) -> bool:
    return bool(_PLACEHOLDER.search(url or ""))


def _step_carries_variable(step: WorkflowStep) -> bool:
    blob = " ".join(
        str(part)
        for part in (
            step.value,
            step.selectedText,
            step.target_text,
            step.url,
            step.description,
        )
        if part
    )
    return bool(_PLACEHOLDER.search(blob))


def _variable_in_path(url: str | None) -> bool:
    if not url:
        return False
    from urllib.parse import urlparse
    return bool(_PLACEHOLDER.search(urlparse(url).path or ""))


def _is_direct_variable_url(url: str | None) -> bool:
    """A URL that is itself the filtered/entity result, not a typeahead query."""
    if _variable_in_path(url):
        return True
    return _is_search_url(url)


def _is_search_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    path = (parsed.path or "").lower()
    if "search" in query or "title" in {k.lower() for k in query}:
        return True
    return "special:search" in path or path.endswith("/search") or "search=" in url.lower()


def _is_junk_input(step: WorkflowStep) -> bool:
    if step.type not in {"input", "select_change"}:
        return False
    value = (step.value if step.value is not None else step.selectedText) or ""
    if str(value).startswith("{"):
        return False
    typ = ""
    for strategy in step.selectorStrategies or []:
        typ = typ + " " + str((strategy.metadata or {}).get("type") or "")
    typ = typ + " " + (step.cssSelector or "") + " " + (step.xpath or "")
    if re.search(r"checkbox|radio|dropdown-checkbox", typ, re.I):
        return True
    return len(str(value).strip()) <= 2


def _is_chrome_step(step: WorkflowStep) -> bool:
    if _is_extract(step) or step.type == "navigation":
        return False
    return bool(_CHROME.search(_blob(step)))


def _same_field(a: WorkflowStep, b: WorkflowStep) -> bool:
    if a.cssSelector and a.cssSelector == b.cssSelector:
        return True
    if a.xpath and a.xpath == b.xpath:
        return True
    if a.target_text and a.target_text == b.target_text:
        return True
    return False


def merge_consecutive_fills(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    out: list[WorkflowStep] = []
    for step in steps:
        if (
            out
            and step.type in {"input", "select_change"}
            and out[-1].type == step.type
            and _same_field(out[-1], step)
        ):
            out[-1] = step
            continue
        out.append(step)
    return out


def drop_junk_steps(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    out = [step for step in steps if not _is_junk_input(step) and not _is_chrome_step(step)]
    return protect_last_action(out, steps)


def collapse_back_and_forth_navigations(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    """Drop consecutive duplicate URLs and simple A→B→A detours with no work on B."""

    # Consecutive same URL
    collapsed: list[WorkflowStep] = []
    for step in steps:
        if (
            step.type == "navigation"
            and collapsed
            and collapsed[-1].type == "navigation"
            and collapsed[-1].url
            and collapsed[-1].url == step.url
        ):
            continue
        collapsed.append(step)

    # Repeatedly drop A, B, A when B is a lone navigation (or nav+chrome we already dropped).
    changed = True
    while changed:
        changed = False
        out: list[WorkflowStep] = []
        i = 0
        while i < len(collapsed):
            a = collapsed[i]
            if (
                a.type == "navigation"
                and i + 2 < len(collapsed)
                and collapsed[i + 1].type == "navigation"
                and collapsed[i + 2].type == "navigation"
                and a.url
                and a.url == collapsed[i + 2].url
                and a.url != collapsed[i + 1].url
            ):
                # Skip the middle navigation; keep one A (the later one).
                out.append(collapsed[i + 2])
                i += 3
                changed = True
                continue
            out.append(a)
            i += 1
        collapsed = out
    return collapsed


def _best_variable_navigation(steps: list[WorkflowStep]) -> WorkflowStep | None:
    navs = [s for s in steps if s.type == "navigation" and _embeds_variable(s.url)]
    if not navs:
        return None
    search = [s for s in navs if _is_search_url(s.url)]
    return search[-1] if search else navs[-1]


def prefer_direct_variable_url(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    """If a captured URL already embeds {term}, start there and keep only the tail to extract."""

    best = _best_variable_navigation(steps)
    if best is None:
        return steps
    # A ?q={team} typeahead URL is not a filtered result. Keep the UI filter steps.
    if not _is_direct_variable_url(best.url):
        return steps
    if any(
        _step_carries_variable(step) and step.type in {"click", "input", "select_change"}
        for step in steps
    ) and not _variable_in_path(best.url) and not _is_search_url(best.url):
        return steps
    # Keep the preferred nav, then non-navigation work that followed the last time we
    # were on a variable URL (or after the last nav), plus extract.
    last_var_index = max(i for i, step in enumerate(steps) if step.type == "navigation" and step.url == best.url)
    tail = [step for step in steps[last_var_index + 1 :] if step.type != "navigation" or _embeds_variable(step.url)]
    # If we preferred a search URL, keep a single later click (first result) if one exists.
    kept_tail: list[WorkflowStep] = []
    clicks = 0
    for step in tail:
        if _is_extract(step):
            kept_tail.append(step)
            continue
        if step.type == "navigation":
            # Later article URL that still embeds the variable is fine; drop others.
            if _embeds_variable(step.url):
                # If we already have a search nav, an article nav after a click is ok.
                kept_tail.append(step)
            continue
        if step.type == "click":
            if _step_carries_variable(step):
                kept_tail.append(step)
                continue
            if _is_search_url(best.url):
                if clicks >= 1:
                    continue
                clicks += 1
                kept_tail.append(step)
            # For a direct article URL, clicks are usually not needed.
            continue
        if step.type in {"input", "select_change", "key_press"}:
            if _step_carries_variable(step) and not _variable_in_path(best.url) and not _is_search_url(best.url):
                kept_tail.append(step)
                continue
            # Typing is redundant when a search or entity URL already carries the variable.
            continue
        kept_tail.append(step)
    if not kept_tail or not _is_extract(kept_tail[-1]):
        extract = next((s for s in reversed(steps) if _is_extract(s)), None)
        if extract is not None:
            kept_tail.append(extract)
        else:
            action = last_action(steps)
            if action is not None and action not in kept_tail:
                ident = getattr(action, "rec_id", None)
                if not ident or not any(getattr(s, "rec_id", None) == ident for s in kept_tail):
                    kept_tail.append(action)
    result = [best, *kept_tail]
    result = ensure_terminal(result, steps)
    if result and (result[-1].type in {"extract", "extract_page_content"} or is_action(result[-1])):
        return result
    return steps


def causal_path_to_extract(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    """Keep the last visit plus any earlier fills that still parameterize the task."""

    if not steps:
        return steps
    extract = next((s for s in reversed(steps) if _is_extract(s)), None)
    terminal = extract or last_action(steps)
    if terminal is None:
        return steps
    term_index = next(
        (
            i
            for i, s in enumerate(steps)
            if s is terminal or (getattr(s, "rec_id", None) and getattr(s, "rec_id", None) == getattr(terminal, "rec_id", None))
        ),
        len(steps) - 1,
    )
    steps = list(steps[: term_index + 1])
    last_nav_index = max((i for i, s in enumerate(steps) if s.type == "navigation"), default=None)
    if last_nav_index is None:
        return [s for s in steps if s is terminal or s.type in {"input", "select_change", "click", "key_press"} or _is_extract(s)]
    prefix = steps[:last_nav_index]
    # Keep earlier parameterized work (fills / var-URLs) and drop pure nav bouncing.
    kept_prefix: list[WorkflowStep] = []
    for step in prefix:
        if step.type == "navigation" and _embeds_variable(step.url):
            kept_prefix.append(step)
        elif step.type in {"input", "select_change"} and (str(step.value or "").startswith("{") or len(str(step.value or "")) > 2):
            kept_prefix.append(step)
        elif step.type == "key_press" and (step.key or "").lower() in {"enter", "return"}:
            kept_prefix.append(step)
        elif step.type == "click" and not _is_chrome_step(step) and kept_prefix and kept_prefix[-1].type in {"input", "select_change", "key_press"}:
            kept_prefix.append(step)
    tail = [steps[last_nav_index], *[s for s in steps[last_nav_index + 1 :] if s is terminal or not _is_extract(s)]]
    if not tail or not ( _is_extract(tail[-1]) or is_action(tail[-1]) ):
        tail.append(terminal)
    # If the last nav does not embed a variable and we have an earlier var-URL, prefer_direct will handle it.
    return [*kept_prefix, *tail]


def compact_steps(steps: list[WorkflowStep]) -> tuple[list[WorkflowStep], dict[str, Any]]:
    """Deterministically prune a recorded step list. Returns (steps, stats)."""

    original = len(steps)
    pipeline = [
        ("drop_junk", drop_junk_steps),
        ("merge_fills", merge_consecutive_fills),
        ("collapse_navigations", collapse_back_and_forth_navigations),
        ("prefer_direct_url", prefer_direct_variable_url),
        ("causal_path", causal_path_to_extract),
        ("merge_fills_again", merge_consecutive_fills),
        ("collapse_navigations_again", collapse_back_and_forth_navigations),
    ]
    current = list(steps)
    stages: list[dict[str, Any]] = []
    for name, fn in pipeline:
        before = len(current)
        current = ensure_terminal(fn(current), steps)
        stages.append({"name": name, "before": before, "after": len(current)})
    stats = {
        "method": "deterministic",
        "steps_before": original,
        "steps_after": len(current),
        "stages": stages,
    }
    return current, stats


def _step_brief(index: int, step: WorkflowStep) -> dict[str, Any]:
    return {
        "index": index,
        "type": step.type,
        "url": step.url,
        "value": step.value or step.selectedText,
        "key": step.key,
        "description": step.description,
        "cssSelector": step.cssSelector,
        "target_text": step.target_text,
    }


async def maybe_llm_prune(
    steps: list[WorkflowStep],
    *,
    task: str,
    llm: Any | None,
) -> tuple[list[WorkflowStep], dict[str, Any]]:
    """Optional cheap single-pass prune. Invalid or failed responses keep `steps`."""

    empty = {"used": False, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    if len(steps) <= 3:
        return steps, empty
    try:
        import httpx
    except Exception:
        return steps, empty
    import os
    api_key = None
    model = "qwen/qwen3.7-flash"
    if llm is not None:
        api_key = llm.key() if hasattr(llm, "key") else None
        model = getattr(llm, "model", None) or model
    if not api_key:
        api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip() or None
    if not api_key:
        return steps, empty
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 300,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Prune a recorded browser workflow to the shortest causal path that still "
                    "accomplishes the task. Reply with JSON only: {\"keep\": [indices]}. "
                    "Indices are 0-based and in order. Keep the final extract step if one exists. "
                    "If there is no extract, you MUST keep the final click, submit, fill, select, or Enter "
                    "and any fill or select steps it depends on. Never drop the last meaningful action. "
                    "Only drop steps; never invent URLs or selectors. Prefer one navigation "
                    "whose URL path already embeds a {variable} (for example /wiki/{term}) over typing a search when that is enough. "
                    "Never drop a click, select, or typed field that itself uses a {variable} — those are filters and must stay. "
                    "A query like ?q={team} is not enough to drop filter steps. "
                    "Drop back-and-forth navigations, menus, and junk."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"task": task, "steps": [_step_brief(i, s) for i, s in enumerate(steps)]}),
            },
        ],
    }
    tokens_in = tokens_out = 0
    cost = None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        usage = body.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        cost = usage.get("cost")
        text = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
        data = json.loads(text)
        keep = data.get("keep") if isinstance(data, dict) else data
        if not isinstance(keep, list) or not keep:
            raise ValueError("empty keep")
        indices = [int(i) for i in keep]
        if any(i < 0 or i >= len(steps) for i in indices):
            raise ValueError("index out of range")
        pruned = [steps[i] for i in indices]
        if _is_extract(steps[-1]) and not _is_extract(pruned[-1]):
            raise ValueError("pruned list dropped the extract step")
        if not _is_extract(pruned[-1]) and not is_action(pruned[-1]):
            raise ValueError("pruned list must end with extract or a final action")
        original_term = last_terminal(steps)
        pruned_term = last_terminal(pruned)
        if original_term is not None and (pruned_term is None or (
            getattr(original_term, "rec_id", None)
            and getattr(original_term, "rec_id", None) != getattr(pruned_term, "rec_id", None)
            and original_term.type != pruned_term.type
        )):
            raise ValueError("pruned list dropped the final action")
        if original_term is not None and is_action(original_term):
            ident = getattr(original_term, "rec_id", None)
            if ident and not any(getattr(s, "rec_id", None) == ident for s in pruned):
                # Allow keeping the same action even if rec_id was lost, but not dropping it.
                if pruned_term is None or pruned_term.type != original_term.type or (original_term.target_text and original_term.target_text != pruned_term.target_text):
                    raise ValueError("pruned list dropped the final action")
        if not any(s.type == "navigation" or s.type in {"input", "click"} for s in pruned[:-1]) and len(pruned) < 2:
            raise ValueError("too aggressive")
        original_filters = [s for s in steps if _step_carries_variable(s) and s.type in {"click", "input", "select_change"}]
        kept_filters = [s for s in pruned if _step_carries_variable(s) and s.type in {"click", "input", "select_change"}]
        if original_filters and not kept_filters:
            raise ValueError("dropped parameterized filter steps")
    except Exception:
        return steps, {
            "used": False,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": estimate_cost_usd(tokens_in, tokens_out, cost) or 0.0,
            "error": "llm_prune_rejected_or_failed",
        }
    return pruned, {
        "used": True,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": estimate_cost_usd(tokens_in, tokens_out, cost) or 0.0,
    }


def _rec_id(step: WorkflowStep) -> str:
    return str(getattr(step, "rec_id", None) or "")


def stamp_rec_ids(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    out: list[WorkflowStep] = []
    for i, step in enumerate(steps):
        if _rec_id(step):
            out.append(step)
            continue
        data = step.model_dump()
        data["rec_id"] = f"s{i}"
        out.append(WorkflowStep.model_validate(data))
    return out


def collapse_redirect_chains(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    """Keep the last navigation in a run of back-to-back redirects with no work in between."""
    out: list[WorkflowStep] = []
    for step in steps:
        if step.type == "navigation" and out and out[-1].type == "navigation":
            out[-1] = step
            continue
        out.append(step)
    return out


def _click_key(step: WorkflowStep) -> str:
    return " ".join(
        part
        for part in (
            (step.cssSelector or "").strip(),
            (step.xpath or "").strip(),
            (step.target_text or "").strip().lower(),
        )
        if part
    )


def dedupe_consecutive_clicks(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    out: list[WorkflowStep] = []
    for step in steps:
        if step.type == "click" and out and out[-1].type == "click" and _click_key(step) and _click_key(step) == _click_key(out[-1]):
            out[-1] = step
            continue
        out.append(step)
    return protect_last_action(out, steps)


_PASSIVE_TAGS = {"html", "body", "main", "p", "h1", "h2", "h3", "h4", "h5", "h6", "div", "span", "section", "article", "header", "footer", "img", "svg"}
_ACTIVE_ROLES = {"button", "link", "option", "tab", "menuitem", "checkbox", "radio", "combobox", "textbox", "searchbox"}


def _is_passive_click(step: WorkflowStep) -> bool:
    if step.type != "click":
        return False
    if _step_carries_variable(step):
        return False
    changed = getattr(step, "changed", None)
    if changed is False:
        return True
    el = getattr(step, "element", None) or {}
    if not isinstance(el, dict):
        el = {}
    tag = str(el.get("tag") or "").lower()
    role = str(el.get("role") or "").lower()
    href = el.get("href")
    typ = str(el.get("type") or "").lower()
    if typ in {"submit", "button"} or href or role in _ACTIVE_ROLES:
        return False
    if tag in {"a", "button", "input", "select", "textarea", "label", "summary"}:
        return False
    text = (step.target_text or el.get("text") or "").strip()
    if tag in _PASSIVE_TAGS and not href and role not in _ACTIVE_ROLES and (not text or len(text) > 80 or text.lower() in {"skip to content", "skip to main content"}):
        return True
    if changed is False:
        return True
    return False


def drop_noop_clicks(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    """Drop focus / empty clicks and clicks that recorded no URL or DOM change.

    A click immediately followed by a navigation to a different URL is kept
    even if the in-page `changed` flag missed the load.
    """
    out: list[WorkflowStep] = []
    for i, step in enumerate(steps):
        if step.type != "click":
            out.append(step)
            continue
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        caused_nav = bool(nxt and nxt.type == "navigation" and nxt.url)
        if caused_nav and getattr(step, "changed", None) is not False:
            out.append(step)
            continue
        if caused_nav and getattr(step, "url", None) and nxt and nxt.url and nxt.url.split("#")[0] != str(step.url or "").split("#")[0]:
            out.append(step)
            continue
        if _is_passive_click(step) or getattr(step, "changed", None) is False:
            continue
        out.append(step)
    return protect_last_action(out, steps)


def compact_recording_steps(steps: list[WorkflowStep]) -> tuple[list[WorkflowStep], dict[str, Any], list[dict[str, Any]]]:
    """Deterministic prune for a human recording, with a removed-step audit trail."""

    original = stamp_rec_ids(list(steps))
    by_id = {_rec_id(step): step for step in original if _rec_id(step)}
    pipeline = [
        ("drop_junk", drop_junk_steps),
        ("drop_noop_clicks", drop_noop_clicks),
        ("dedupe_clicks", dedupe_consecutive_clicks),
        ("merge_fills", merge_consecutive_fills),
        ("collapse_redirects", collapse_redirect_chains),
        ("collapse_navigations", collapse_back_and_forth_navigations),
        ("prefer_direct_url", prefer_direct_variable_url),
        ("causal_path", causal_path_to_extract),
        ("merge_fills_again", merge_consecutive_fills),
        ("collapse_navigations_again", collapse_back_and_forth_navigations),
        ("dedupe_clicks_again", dedupe_consecutive_clicks),
    ]
    current = list(original)
    stages: list[dict[str, Any]] = []
    reasons: dict[str, str] = {}
    for name, fn in pipeline:
        before = current
        after = ensure_terminal(fn(current), original)
        kept_ids = {_rec_id(step) for step in after}
        for step in before:
            ident = _rec_id(step)
            if ident and ident not in kept_ids and ident not in reasons:
                reasons[ident] = name
        current = after
        stages.append({"name": name, "before": len(before), "after": len(current)})
    kept_ids = {_rec_id(step) for step in current}
    removed = []
    for step in original:
        ident = _rec_id(step)
        if ident and ident not in kept_ids:
            removed.append({"id": ident, "reason": reasons.get(ident) or "pruned", "step": step.model_dump(exclude_none=True)})
    stats = {
        "method": "deterministic",
        "steps_before": len(original),
        "steps_after": len(current),
        "stages": stages,
    }
    return current, stats, removed
