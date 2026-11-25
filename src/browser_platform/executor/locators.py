"""Map a SelectorStrategy onto a Playwright Locator."""

from __future__ import annotations

from playwright.async_api import Locator, Page

from browser_platform.models.workflow import SelectorStrategy

_ROLE_ALIASES = {
    "a": "link",
    "link": "link",
    "button": "button",
    "btn": "button",
    "heading": "heading",
    "h1": "heading",
    "h2": "heading",
    "textbox": "textbox",
    "input": "textbox",
    "searchbox": "searchbox",
    "checkbox": "checkbox",
    "radio": "radio",
    "combobox": "combobox",
    "listbox": "listbox",
    "option": "option",
    "tab": "tab",
    "menuitem": "menuitem",
}


def locator_for(page: Page, strategy: SelectorStrategy) -> Locator:
    typ = (strategy.type or "").lower()
    value = strategy.value or ""
    meta = strategy.metadata or {}
    role = str(meta.get("role") or meta.get("tag") or "").lower()
    role = _ROLE_ALIASES.get(role, role)

    if typ in {"cached"}:
        # A cached strategy should already have the real type in metadata or value as css.
        inner = str(meta.get("original_type") or "css")
        return locator_for(page, SelectorStrategy(type=inner, value=value, metadata=meta, priority=1))

    if typ in {"id", "css_id"}:
        sel = value if value.startswith("#") else f"#{value.lstrip('#')}"
        return page.locator(sel)

    if typ in {"css", "cssselector", "css_selector", "css_attr", "css_class"}:
        return page.locator(value)

    if typ in {"xpath"}:
        expr = value if value.startswith("/") or value.startswith("(") or value.startswith("xpath=") else value
        if expr.startswith("xpath="):
            return page.locator(expr)
        return page.locator(f"xpath={expr}")

    if typ in {"text_exact", "text"}:
        return page.get_by_text(value, exact=True)

    if typ in {"text_fuzzy"}:
        return page.get_by_text(value, exact=False)

    if typ in {"role_text", "role"}:
        role_name = role or "button"
        try:
            return page.get_by_role(role_name, name=value)  # type: ignore[arg-type]
        except Exception:
            return page.get_by_text(value, exact=False)

    if typ in {"aria_label", "aria"}:
        return page.locator(f"[aria-label={_quote(value)}]")

    if typ in {"placeholder"}:
        return page.get_by_placeholder(value)

    if typ in {"title"}:
        return page.locator(f"[title={_quote(value)}]")

    if typ in {"alt_text", "alt"}:
        return page.get_by_alt_text(value)

    if typ in {"name", "attr_name"}:
        return page.locator(f"[name={_quote(value)}]")

    if typ in {"label"}:
        return page.get_by_label(value)

    if typ in {"test_id", "data_testid"}:
        return page.get_by_test_id(value)

    # Fallback: treat value as CSS, then text.
    if value:
        return page.locator(value)
    return page.locator("html")


def _quote(value: str) -> str:
    if '"' in value and "'" in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if '"' in value:
        return f"'{value}'"
    return f'"{value}"'
