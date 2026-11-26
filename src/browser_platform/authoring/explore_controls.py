"""Discover optional explore-mode inputs from HTML controls and URL patterns."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from browser_platform.interpolate import current_year_month, shift_year_month
from browser_platform.workflow_inputs import pretty_label

MAX_PROPOSALS = 6


def html_attr(attrs: str, name: str) -> str:
    match = re.search(
        r"(?:^|\s)" + re.escape(name) + r'\s*=\s*(["\']?)([^"\'>]*)\1',
        attrs or "",
        re.I,
    )
    return (match.group(2) if match else "").strip()


def _unique_name(candidates: list[str], fallback: str, used: set[str]) -> str:
    for raw in candidates:
        slug = re.sub(r"[^a-z0-9_]+", "_", (raw or "").strip().lower()).strip("_")
        if slug in {"select", "input", "q", "v", "value", "dropdown", "menu"}:
            continue
        if slug and slug not in used:
            return slug
    name = fallback
    i = 2
    while name in used:
        name = f"{fallback}_{i}"
        i += 1
    return name


def propose_from_url_patterns(url: str, *, existing: set[str], step_index: int, seen: set[str]) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    if not url:
        return proposals
    page_m = re.search(r"(page[-/])(\d+)", url, re.I)
    if page_m and "page" not in seen and "page" not in existing:
        current = page_m.group(2)
        alt = "2" if current != "2" else "3"
        proposals.append(
            {
                "name": "page",
                "label": "Page",
                "required": False,
                "default": current,
                "description": "Optional results page. Defaults to the current page.",
                "binding": {
                    "kind": "url_replace",
                    "step_index": step_index,
                    "replace": page_m.group(0),
                    "placeholder": page_m.group(1) + "{page}",
                    "source_url": url,
                },
                "reason": "Pagination appears in the URL path.",
                "verify_with": alt,
            }
        )
        seen.add("page")
    q_page = re.search(r"([?&](?:page|p|pg|pagenum)=)(\d+)", url, re.I)
    if q_page and "page" not in seen and "page" not in existing:
        current = q_page.group(2)
        alt = "2" if current != "2" else "3"
        proposals.append(
            {
                "name": "page",
                "label": "Page",
                "required": False,
                "default": current,
                "binding": {
                    "kind": "url_replace",
                    "step_index": step_index,
                    "replace": q_page.group(0),
                    "placeholder": q_page.group(1) + "{page}",
                    "source_url": url,
                },
                "reason": "A page query parameter can be varied.",
                "verify_with": alt,
            }
        )
        seen.add("page")
    q_sort = re.search(r"([?&](?:sort|order|orderby)=)([^&]*)", url, re.I)
    if q_sort and "sort" not in seen and "sort" not in existing:
        current = q_sort.group(2)
        proposals.append(
            {
                "name": "sort",
                "label": "Sort",
                "required": False,
                "default": current,
                "binding": {
                    "kind": "url_replace",
                    "step_index": step_index,
                    "replace": q_sort.group(0),
                    "placeholder": q_sort.group(1) + "{sort}",
                    "source_url": url,
                },
                "reason": "A sort query parameter can be varied.",
                "verify_with": None,
            }
        )
        seen.add("sort")
    cat = re.search(r"/category/books/([^/]+)/", url, re.I)
    if cat and "category" not in seen and "category" not in existing:
        current = cat.group(1)
        proposals.append(
            {
                "name": "category",
                "label": "Category",
                "required": False,
                "default": current,
                "binding": {
                    "kind": "url_replace",
                    "step_index": step_index,
                    "replace": current,
                    "placeholder": "{category}",
                    "source_url": url,
                },
                "reason": "A category path segment can be varied.",
                "verify_with": "mystery_3" if current != "mystery_3" else "travel_2",
            }
        )
        seen.add("category")
    return proposals


def propose_from_html(html: str, url: str, *, existing: set[str] | None = None, step_index: int = 0) -> list[dict[str, Any]]:
    """Optional inputs from selects, pagination links, tabs, radios, and date fields."""

    existing = {str(n).lower() for n in (existing or set())}
    proposals: list[dict[str, Any]] = []
    seen: set[str] = set()
    html = html or ""
    url = url or ""

    page_links = re.findall(r'href=["\']([^"\']*page[-/_]?(\d+)[^"\']*)["\']', html, re.I)
    if page_links and "page" not in existing and "page" not in seen:
        template = None
        for href, _num in page_links:
            if re.search(r"page[-/]\d+", href, re.I):
                template = re.sub(r"(page[-/])\d+", r"\1{page}", href, count=1, flags=re.I)
                break
        if template:
            template = urljoin(url or "https://example.com/", template)
            here = re.search(r"page[-/](\d+)", url, re.I)
            current = here.group(1) if here else "1"
            alt = "2" if current != "2" else "3"
            proposals.append(
                {
                    "name": "page",
                    "label": "Page",
                    "required": False,
                    "default": current,
                    "description": "Optional results page. Defaults to the current page.",
                    "binding": {"kind": "url_template", "step_index": step_index, "template": template, "source_url": url},
                    "reason": "Pagination links on the page map to a URL template.",
                    "verify_with": alt,
                }
            )
            seen.add("page")

    for sel_m in re.finditer(r"<select\b([^>]*)>(.*?)</select>", html, re.I | re.S):
        attrs, body = sel_m.group(1), sel_m.group(2)
        options: list[dict[str, Any]] = []
        selected = None
        for opt in re.finditer(r"<option\b([^>]*)>(.*?)</option>", body, re.I | re.S):
            oattrs, text = opt.group(1), re.sub(r"<[^>]+>", "", opt.group(2)).strip()
            value = html_attr(oattrs, "value") or text
            is_sel = "selected" in oattrs.lower()
            item = {"value": value, "text": text, "selected": is_sel}
            options.append(item)
            if is_sel:
                selected = item
        if len(options) < 2:
            continue
        if selected is None:
            selected = options[0]
        name = _unique_name(
            [
                html_attr(attrs, "name"),
                html_attr(attrs, "id"),
                html_attr(attrs, "aria-label"),
                "sort" if re.search(r"sort|order", attrs + body, re.I) else "",
            ],
            "option",
            existing | seen,
        )
        alt = next((o["text"] or o["value"] for o in options if (o["text"] or o["value"]) not in {selected.get("text"), selected.get("value")}), None)
        if not alt:
            continue
        el_id = html_attr(attrs, "id")
        el_name = html_attr(attrs, "name")
        el_class = html_attr(attrs, "class")
        css = f"#{el_id}" if el_id else (f"select[name='{el_name}']" if el_name else None)
        if not css and el_class:
            css = "select." + ".".join(part for part in el_class.split() if part)
        proposals.append(
            {
                "name": name,
                "label": pretty_label(name),
                "required": False,
                "default": selected.get("text") or selected.get("value"),
                "description": f"Optional {pretty_label(name).lower()} control. Defaults to the currently selected value.",
                "binding": {"kind": "select", "step_index": step_index, "css": css, "id": el_id or None, "options": options},
                "reason": f"A <select> with {len(options)} options can be varied deterministically.",
                "verify_with": alt,
                "verify_mode": "browser",
            }
        )
        seen.add(name)
        if len(proposals) >= MAX_PROPOSALS:
            break

    tab_texts = re.findall(
        r'<(?:a|button)[^>]*(?:role=["\']tab["\']|class=["\'][^"\']*(?:\btab\b|filter-pill)[^"\']*)[^>]*>\s*([^<]{2,40})\s*</(?:a|button)>',
        html,
        re.I,
    )
    if not tab_texts:
        tab_texts = re.findall(
            r'href=["\'][^"\']*(?:competition|league|filter)[^"\']*["\'][^>]*>\s*([^<]{3,40})\s*<',
            html,
            re.I,
        )
    cleaned = [re.sub(r"\s+", " ", item).strip() for item in tab_texts if item and str(item).strip()]
    cleaned = list(dict.fromkeys(cleaned))
    if len(cleaned) >= 2:
        name = "competition" if any(re.search(r"league|premier|cup|championship|competition", item, re.I) for item in cleaned) else "tab"
        if name not in existing and name not in seen:
            proposals.append(
                {
                    "name": name,
                    "label": pretty_label(name),
                    "required": False,
                    "default": cleaned[0],
                    "description": f"Optional {pretty_label(name).lower()} tab. Defaults to the current tab.",
                    "binding": {"kind": "tab", "step_index": step_index, "options": cleaned},
                    "reason": "Tab or competition filters on the page can be chosen by visible name.",
                    "verify_with": cleaned[1],
                    "verify_mode": "browser",
                }
            )
            seen.add(name)

    radios: dict[str, list[str]] = {}
    selected_radio: dict[str, str] = {}
    for rm in re.finditer(r'<input\b([^>]*type=["\']radio["\'][^>]*)>', html, re.I):
        attrs = rm.group(1)
        gname = html_attr(attrs, "name") or "choice"
        val = html_attr(attrs, "value") or html_attr(attrs, "id") or ""
        if not val:
            continue
        radios.setdefault(gname, [])
        if val not in radios[gname]:
            radios[gname].append(val)
        if "checked" in attrs.lower():
            selected_radio[gname] = val
    for gname, values in radios.items():
        if len(values) < 2:
            continue
        name = _unique_name([gname, "choice"], "choice", existing | seen)
        default = selected_radio.get(gname) or values[0]
        alt = next((item for item in values if item != default), None)
        if not alt:
            continue
        proposals.append(
            {
                "name": name,
                "label": pretty_label(name),
                "required": False,
                "default": default,
                "binding": {"kind": "radio", "step_index": step_index, "css": f"input[name='{gname}'][value='{{{name}}}']", "options": values},
                "reason": "A radio group can be chosen by value.",
                "verify_with": alt,
                "verify_mode": "browser",
            }
        )
        seen.add(name)

    if re.search(r'<input\b[^>]*type=["\'](?:date|month)["\']', html, re.I) and "month" not in existing and "date" not in existing:
        typ = "month" if re.search(r'type=["\']month["\']', html, re.I) else "date"
        if typ not in seen:
            proposals.append(
                {
                    "name": typ,
                    "label": pretty_label(typ),
                    "required": False,
                    "runtime_default": "current_month" if typ == "month" else None,
                    "format": "YYYY-MM" if typ == "month" else "YYYY-MM-DD",
                    "description": "Optional date control. Defaults to the current month at run time." if typ == "month" else "Optional date.",
                    "binding": {"kind": "select", "step_index": step_index, "css": f"input[type='{typ}']"},
                    "reason": "A date picker on the page can be bound as an optional input.",
                    "verify_with": shift_year_month(current_year_month(), 1) if typ == "month" else None,
                }
            )
            seen.add(typ)

    return proposals[:MAX_PROPOSALS]
