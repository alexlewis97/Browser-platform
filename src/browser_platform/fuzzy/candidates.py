"""Collect clickable/selectable page candidates and apply a chosen one."""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_COLLECT_JS = r"""
(args) => {
  const { selector, limit, containerHint } = args;
  const escape = (window.CSS && CSS.escape) ? CSS.escape : (s) => String(s).replace(/[^a-zA-Z0-9_\-]/g, '\\$&');
  function shortPath(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return `#${escape(el.id)}`;
    const dataName = el.getAttribute("data-name");
    if (dataName) return `${el.tagName.toLowerCase()}[data-name="${escape(dataName)}"]`;
    const value = el.getAttribute("value");
    if (el.tagName === "OPTION" && value != null) {
      const sel = el.closest("select");
      const root = sel ? shortPath(sel) : "select";
      return `${root} option[value="${escape(value)}"]`;
    }
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement && parts.length < 6) {
      if (cur.id) { parts.unshift(`#${escape(cur.id)}`); break; }
      let part = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(cur) + 1})`;
      }
      parts.unshift(part);
      cur = parent;
    }
    return parts.join(" > ");
  }
  let root = document;
  if (containerHint) {
    try { const hit = document.querySelector(containerHint); if (hit) root = hit; } catch (e) {}
  }
  const nodes = Array.from(root.querySelectorAll(selector)).slice(0, limit);
  return nodes.map((el) => {
    const text = (el.innerText || el.textContent || el.getAttribute("aria-label") || el.getAttribute("label") || el.value || "")
      .replace(/\s+/g, " ")
      .trim();
    return {
      text,
      role: el.getAttribute("role") || el.tagName.toLowerCase(),
      tag: el.tagName.toLowerCase(),
      value: el.value != null ? String(el.value) : null,
      selector: shortPath(el),
      disabled: !!(el.disabled || el.getAttribute("aria-disabled") === "true"),
    };
  }).filter((c) => c.text);
}
"""

_DEFAULT_SELECTOR = ",".join(
    [
        "option",
        "[role='option']",
        "tbody tr",
        "table tr",
        "[role='row']",
        "li",
        "[role='listitem']",
        "label",
        "button",
        "[role='button']",
        "a[href]",
        "[role='link']",
        "[role='menuitem']",
        "[role='tab']",
        "[role='radio']",
        "[role='checkbox']",
    ]
)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    text: str
    role: str | None = None
    selector: str | None = None
    selector_type: str = "css"
    value: str | None = None
    score: float | None = None
    screenshot: str | None = None

    def public_dict(self, *, include_screenshot: bool = True) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "text": self.text,
            "role": self.role,
            "selector": self.selector,
            "selector_type": self.selector_type,
            "value": self.value,
            "score": self.score,
        }
        if include_screenshot and self.screenshot:
            payload["screenshot"] = self.screenshot
        return payload


def _cid(*parts: str | None) -> str:
    raw = "|".join(p or "" for p in parts)
    return "c_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _from_hook(raw: Any, index: int) -> Candidate | None:
    if isinstance(raw, Candidate):
        return raw
    if isinstance(raw, dict):
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        return Candidate(
            id=str(raw.get("id") or _cid(text, raw.get("selector"), str(index))),
            text=text,
            role=raw.get("role"),
            selector=raw.get("selector") or raw.get("css"),
            selector_type=raw.get("selector_type") or "css",
            value=raw.get("value"),
            screenshot=raw.get("screenshot"),
        )
    text = str(raw).strip()
    if not text:
        return None
    return Candidate(id=_cid(text, str(index)), text=text)


async def collect_candidates(
    page: Any,
    step: Any | None = None,
    *,
    include_screenshot: bool = False,
    limit: int = 80,
) -> list[Candidate]:
    """Collect rows/options/labels from a Playwright page, a test hook, or an a11y snapshot."""

    hook = getattr(page, "fuzzy_candidates", None)
    if hook is not None:
        items = hook() if callable(hook) else hook
        out = []
        for i, raw in enumerate(items or []):
            cand = _from_hook(raw, i)
            if cand:
                out.append(cand)
        return out

    selector = _DEFAULT_SELECTOR
    typ = getattr(step, "type", None)
    if typ == "select_change":
        selector = "option, [role='option']"
    container = getattr(step, "container_hint", None) if step is not None else None

    records: list[dict[str, Any]] = []
    try:
        if hasattr(page, "evaluate"):
            records = await page.evaluate(
                _COLLECT_JS,
                {"selector": selector, "limit": limit, "containerHint": container},
            )
    except Exception:
        records = []

    # Merge accessibility names when the DOM pass is thin.
    if len(records) < 3:
        try:
            from browser_platform.a11y import snapshot_ax
            snapshot = await snapshot_ax(page)
            records.extend(_flatten_ax(snapshot))
        except Exception:
            pass

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for i, rec in enumerate(records or []):
        if rec.get("disabled"):
            continue
        text = " ".join(str(rec.get("text") or "").split())
        if not text:
            continue
        selector_val = rec.get("selector")
        key = f"{text}|{selector_val or ''}"
        if key in seen:
            continue
        seen.add(key)
        cand = Candidate(
            id=_cid(text, selector_val, rec.get("role"), str(i)),
            text=text,
            role=rec.get("role") or rec.get("tag"),
            selector=selector_val,
            value=rec.get("value"),
        )
        candidates.append(cand)

    if include_screenshot:
        await _attach_screenshots(page, candidates)
    return candidates


def _flatten_ax(node: dict[str, Any] | None, acc: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    acc = acc if acc is not None else []
    if not node:
        return acc
    role = str(node.get("role") or "").strip()
    name = " ".join(str(node.get("name") or "").split())
    if name and role.lower() not in {"none", "presentation", "ignored", "generic", "document", "WebArea"}:
        acc.append({"text": name, "role": role, "selector": None})
    for child in node.get("children") or []:
        if isinstance(child, dict):
            _flatten_ax(child, acc)
    return acc


async def _attach_screenshots(page: Any, candidates: list[Candidate], *, max_n: int = 5) -> None:
    for cand in candidates[:max_n]:
        if not cand.selector or not hasattr(page, "locator"):
            continue
        try:
            loc = page.locator(cand.selector)
            if await loc.count() < 1:
                continue
            raw = await loc.first.screenshot(type="png")
            import base64

            cand.screenshot = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        except Exception:
            continue


async def apply_candidate(page: Any, step: Any, candidate: Candidate, *, timeout_ms: int = 8000) -> None:
    """Click or select the chosen candidate on the page."""

    hook = getattr(page, "apply_fuzzy_candidate", None)
    if hook is not None:
        result = hook(step, candidate)
        if hasattr(result, "__await__"):
            await result
        return

    typ = getattr(step, "type", "click")
    locator = None
    if candidate.selector and hasattr(page, "locator"):
        locator = page.locator(candidate.selector)
        try:
            if await locator.count() < 1:
                locator = None
        except Exception:
            locator = None
    if locator is None and hasattr(page, "get_by_text"):
        locator = page.get_by_text(candidate.text, exact=True)
    if locator is None:
        raise RuntimeError(f"could not resolve candidate {candidate.id}")

    if typ == "select_change":
        # Prefer selecting by label on the enclosing <select>, fall back to clicking the option.
        try:
            handle = locator.first
            tag = ""
            try:
                tag = (await handle.evaluate("el => el.tagName && el.tagName.toLowerCase()")) or ""
            except Exception:
                tag = (candidate.role or "").lower()
            if tag == "option" or (candidate.role or "").lower() == "option":
                parent = page.locator(candidate.selector).locator("xpath=ancestor::select[1]") if candidate.selector else None
                if parent is not None and await parent.count():
                    label = candidate.text
                    value = candidate.value
                    if value:
                        await parent.first.select_option(value=value, timeout=timeout_ms)
                    else:
                        await parent.first.select_option(label=label, timeout=timeout_ms)
                    return
            await locator.first.select_option(label=candidate.text, timeout=timeout_ms)
            return
        except Exception:
            await locator.first.click(timeout=timeout_ms)
            return
    if typ == "input":
        await locator.first.fill(candidate.text, timeout=timeout_ms)
        return
    await locator.first.click(timeout=timeout_ms)
