"""Turn a recorded DOM element description into workflow-use selectorStrategies."""

from __future__ import annotations

from typing import Any

from browser_platform.models.workflow import SelectorStrategy

_JS_DESCRIBE = r"""
(el) => {
  if (!el || el.nodeType !== 1) return null;
  const escape = (window.CSS && CSS.escape) ? CSS.escape : (s) => String(s).replace(/[^a-zA-Z0-9_\-]/g, '\\$&');

  function stableSelector(node) {
    if (!node || node.nodeType !== 1) return "";
    if (node.id) return `#${escape(node.id)}`;
    for (const attr of ["data-test", "data-testid", "data-cy"]) {
      const v = node.getAttribute(attr);
      if (v) return `[${attr}="${escape(v)}"]`;
    }
    const name = node.getAttribute("name");
    if (name) {
      const tag = node.tagName.toLowerCase();
      const typ = (node.getAttribute("type") || "").toLowerCase();
      const val = node.getAttribute("value");
      if ((typ === "radio" || typ === "checkbox") && val) {
        return `${tag}[name="${escape(name)}"][value="${escape(val)}"]`;
      }
      return `${tag}[name="${escape(name)}"]`;
    }
    return "";
  }
  function cssPath(node) {
    if (!node || node.nodeType !== 1) return "";
    const direct = stableSelector(node);
    if (direct) return direct;
    const parts = [];
    let cur = node;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement && parts.length < 6) {
      const stab = stableSelector(cur);
      if (stab) { parts.unshift(stab); break; }
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
  function xpath(node) {
    if (node.id) return `//*[@id="${node.id}"]`;
    const parts = [];
    let cur = node;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement) {
      let ix = 1;
      let sib = cur.previousElementSibling;
      while (sib) { if (sib.tagName === cur.tagName) ix += 1; sib = sib.previousElementSibling; }
      parts.unshift(`${cur.tagName.toLowerCase()}[${ix}]`);
      cur = cur.parentElement;
    }
    return "//" + parts.join("/");
  }
  const text = ((el.innerText || el.textContent || "").replace(/\s+/g, " ").trim()).slice(0, 160);
  const role = el.getAttribute("role") || "";
  const name = el.getAttribute("aria-label") || el.getAttribute("name") || el.getAttribute("placeholder") || "";
  const tag = el.tagName.toLowerCase();
  const implicitRole = role || ({ a: "link", button: "button", input: "textbox", select: "combobox", textarea: "textbox" }[tag] || tag);
  return {
    tag,
    id: el.id || null,
    role: implicitRole,
    name: (name || text || "").slice(0, 160),
    text,
    value: el.value != null ? String(el.value) : null,
    placeholder: el.getAttribute("placeholder"),
    ariaLabel: el.getAttribute("aria-label"),
    type: el.getAttribute("type"),
    href: el.getAttribute("href"),
    htmlFor: el.getAttribute("for") || el.htmlFor || null,
    htmlName: el.getAttribute("name") || null,
    dataTest: el.getAttribute("data-test") || null,
    dataTestId: el.getAttribute("data-testid") || null,
    dataCy: el.getAttribute("data-cy") || null,
    css: cssPath(el),
    xpath: xpath(el),
  };
}
"""

RECORDER_JS = r"""
(() => {
  if (window.__bpRecorder) return;
  const escape = (window.CSS && CSS.escape) ? CSS.escape : (s) => String(s).replace(/[^a-zA-Z0-9_\-]/g, '\\$&');

  function stableSelector(node) {
    if (!node || node.nodeType !== 1) return "";
    if (node.id) return `#${escape(node.id)}`;
    for (const attr of ["data-test", "data-testid", "data-cy"]) {
      const v = node.getAttribute(attr);
      if (v) return `[${attr}="${escape(v)}"]`;
    }
    const name = node.getAttribute("name");
    if (name) {
      const tag = node.tagName.toLowerCase();
      const typ = (node.getAttribute("type") || "").toLowerCase();
      const val = node.getAttribute("value");
      if ((typ === "radio" || typ === "checkbox") && val) {
        return `${tag}[name="${escape(name)}"][value="${escape(val)}"]`;
      }
      return `${tag}[name="${escape(name)}"]`;
    }
    return "";
  }
  function cssPath(node) {
    if (!node || node.nodeType !== 1) return "";
    const direct = stableSelector(node);
    if (direct) return direct;
    const parts = [];
    let cur = node;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement && parts.length < 6) {
      const stab = stableSelector(cur);
      if (stab) { parts.unshift(stab); break; }
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
  function xpath(node) {
    if (!node || node.nodeType !== 1) return "";
    if (node.id) return `//*[@id="${node.id}"]`;
    const parts = [];
    let cur = node;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement) {
      let ix = 1;
      let sib = cur.previousElementSibling;
      while (sib) { if (sib.tagName === cur.tagName) ix += 1; sib = sib.previousElementSibling; }
      parts.unshift(`${cur.tagName.toLowerCase()}[${ix}]`);
      cur = cur.parentElement;
    }
    return "//" + parts.join("/");
  }
  function isFillable(node) {
    if (!node || node.nodeType !== 1) return false;
    const tag = node.tagName.toLowerCase();
    if (["input", "textarea", "select"].includes(tag)) return true;
    if (node.isContentEditable) return true;
    const role = (node.getAttribute("role") || "").toLowerCase();
    return ["textbox", "searchbox", "combobox", "spinbutton"].includes(role);
  }
  function associatedControl(el) {
    if (!el || el.nodeType !== 1) return el;
    if (isFillable(el)) return el;
    const tag = el.tagName.toLowerCase();
    if (tag === "label") {
      const forId = el.getAttribute("for") || el.htmlFor;
      if (forId) {
        const target = document.getElementById(forId);
        if (target) return associatedControl(target) || target;
      }
      const inner = el.querySelector("input, textarea, select, [contenteditable='true']");
      if (inner) return inner;
    }
    if (el.id) {
      const esc = escape(el.id);
      const via = document.querySelector("[aria-labelledby~='" + esc + "'], [aria-labelledby='" + esc + "']");
      if (via) return associatedControl(via) || via;
    }
    const wrap = el.closest && el.closest("label");
    if (wrap && wrap !== el) return associatedControl(wrap);
    const group = (el.parentElement) || el;
    const sibling = group.querySelector && group.querySelector("input, textarea, select, [contenteditable='true']");
    if (sibling) return sibling;
    return el;
  }
  function describe(el) {
    if (!el || el.nodeType !== 1) return null;
    const text = ((el.innerText || el.textContent || "").replace(/\s+/g, " ").trim()).slice(0, 160);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role") || ({ a: "link", button: "button", input: "textbox", select: "combobox", textarea: "textbox", label: "label" }[tag] || tag);
    const name = el.getAttribute("aria-label") || el.getAttribute("name") || el.getAttribute("placeholder") || text;
    return {
      tag, id: el.id || null, role, name: (name || "").slice(0, 160), text,
      value: el.value != null ? String(el.value) : null,
      placeholder: el.getAttribute("placeholder"),
      ariaLabel: el.getAttribute("aria-label"),
      type: el.getAttribute("type"),
      href: el.getAttribute("href"),
      htmlFor: el.getAttribute("for") || el.htmlFor || null,
      htmlName: el.getAttribute("name") || null,
      dataTest: el.getAttribute("data-test") || null,
      dataTestId: el.getAttribute("data-testid") || null,
      dataCy: el.getAttribute("data-cy") || null,
      css: cssPath(el),
      xpath: xpath(el),
    };
  }
  const events = [];
  const lastValues = new Map();
  function emit(rec) {
    try {
      if (typeof window.__bpPushEvent === "function") window.__bpPushEvent(rec);
    } catch (err) {}
  }
  function push(type, el, extra) {
    const rec = { type, ts: Date.now(), url: location.href, element: describe(el), ...(extra || {}) };
    events.push(rec);
    emit(rec);
  }
  function fieldKey(el) {
    if (!el) return "";
    if (el.id) return "#" + el.id;
    return cssPath(el) || xpath(el) || "";
  }
  function skipField(el) {
    const typ = ((el && el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
    // Hidden/button-like controls are not typed values. Radios and checkboxes are handled as choices.
    return ["hidden", "submit", "button", "image", "file", "reset"].includes(typ);
  }
  function isChoice(el) {
    const typ = ((el && el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
    return typ === "checkbox" || typ === "radio";
  }
  function fieldValue(el) {
    if (!el) return "";
    const tag = (el.tagName || "").toLowerCase();
    const typ = (el.getAttribute("type") || "").toLowerCase();
    if (typ === "checkbox" || typ === "radio") return el.checked ? (el.value || "on") : "";
    if (tag === "select") {
      const opt = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
      return opt ? (opt.text || el.value) : (el.value || "");
    }
    return el.value != null ? String(el.value) : "";
  }
  function noteField(el, reason) {
    el = associatedControl(el);
    if (!el || el.nodeType !== 1 || skipField(el)) return;
    const tag = (el.tagName || "").toLowerCase();
    if (!["input", "textarea", "select"].includes(tag) && !el.isContentEditable && !isChoice(el)) return;
    if (isChoice(el) && reason === "snapshot" && !el.closest("form")) return;
    const typ = (el.getAttribute("type") || "").toLowerCase();
    if (typ === "radio" && !el.checked && reason !== "click") return;
    const key = fieldKey(el);
    if (!key) return;
    const value = fieldValue(el);
    if (lastValues.get(key) === value) return;
    const had = lastValues.has(key);
    lastValues.set(key, value);
    if (!had && value === "") return;
    if (tag === "select" || isChoice(el)) {
      push("select_change", el, { value: value || el.value || "on", selected: el.value || value, source: reason || "diff" });
    } else if (isFillable(el)) {
      push("input", el, { value, source: reason || "diff" });
    }
  }
  function snapshotFields() {
    document.querySelectorAll("input, textarea, select").forEach((el) => noteField(el, "snapshot"));
    return Array.from(lastValues.entries()).map(([k, v]) => ({ key: k, value: v }));
  }
  function hookValue(proto) {
    if (!proto) return;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (!desc || !desc.set || !desc.get || desc.set.__bpHooked) return;
    const origSet = desc.set;
    const hooked = function (v) {
      origSet.call(this, v);
      try { noteField(this, "value_setter"); } catch (err) {}
    };
    hooked.__bpHooked = true;
    Object.defineProperty(proto, "value", { get: desc.get, set: hooked, configurable: true, enumerable: desc.enumerable });
  }
  hookValue(window.HTMLInputElement && HTMLInputElement.prototype);
  hookValue(window.HTMLTextAreaElement && HTMLTextAreaElement.prototype);
  hookValue(window.HTMLSelectElement && HTMLSelectElement.prototype);
  document.addEventListener("click", (e) => {
    if (!e.target) return;
    const control = associatedControl(e.target);
    if (control && isChoice(control)) {
      noteField(control, "click");
      snapshotFields();
      return;
    }
    snapshotFields();
    push("click", e.target);
  }, true);
  document.addEventListener("dblclick", (e) => {
    if (!e.target) return;
    push("double_click", e.target);
  }, true);
  document.addEventListener("contextmenu", (e) => {
    if (!e.target) return;
    push("right_click", e.target);
  }, true);
  document.addEventListener("input", (e) => {
    const el = associatedControl(e.target);
    if (!el || !isFillable(el)) return;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "select") return;
    lastValues.set(fieldKey(el), fieldValue(el));
    push("input", el, { value: el.value, source: "input" });
  }, true);
  document.addEventListener("change", (e) => {
    const el = associatedControl(e.target);
    if (!el) return;
    if (isChoice(el)) {
      lastValues.set(fieldKey(el), fieldValue(el));
      if (el.checked) {
        push("select_change", el, { value: el.value || "on", selected: el.value || "on", source: "change" });
      } else if ((el.getAttribute("type") || "").toLowerCase() === "checkbox") {
        push("select_change", el, { value: "", selected: "", source: "change" });
      }
      return;
    }
    if (!isFillable(el)) return;
    const tag = (el.tagName || "").toLowerCase();
    lastValues.set(fieldKey(el), fieldValue(el));
    if (tag === "select") push("select_change", el, { value: el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : el.value, selected: el.value, source: "change" });
    else push("input", el, { value: el.value, source: "change" });
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === "Tab" || e.key === "Escape") {
      snapshotFields();
      push("key_press", e.target, { key: e.key });
    }
  }, true);
  setInterval(snapshotFields, 200);
  window.addEventListener("pagehide", () => {
    snapshotFields();
    try {
      if (typeof window.__bpPushEvent === "function" && events.length) {
        window.__bpPushEvent(events.slice());
        events.length = 0;
      }
    } catch (err) {}
  }, true);
  window.__bpRecorder = {
    events,
    drain() { snapshotFields(); const out = events.slice(); events.length = 0; return out; },
    snapshot() { return events.slice(); },
    snapshotFields,
  };
})();
"""


def is_label_element(element: dict[str, Any] | None) -> bool:
    """True when the captured node is a <label> (or a label-like stand-in), not a fillable field."""

    if not element:
        return False
    tag = str(element.get("tag") or "").lower()
    role = str(element.get("role") or "").lower()
    typ = str(element.get("type") or "").lower()
    el_id = str(element.get("id") or "")
    if tag in {"input", "textarea", "select"} or typ in {"text", "email", "password", "tel", "search", "number", "date"}:
        return False
    if tag == "label" or role == "label":
        return True
    if el_id.lower().endswith("-label") or el_id.lower().endswith("_label") or el_id.lower().endswith("label"):
        return True
    return False


def label_input_selectors(element: dict[str, Any] | None) -> list[str]:
    """CSS selectors that resolve a label to its associated input."""

    if not element:
        return []
    selectors: list[str] = []
    for_id = element.get("htmlFor") or element.get("for")
    el_id = element.get("id")
    if for_id:
        selectors.append(f"#{for_id}")
    if el_id:
        selectors.append(f"[aria-labelledby='{el_id}']")
        selectors.append(f"label#{el_id} ~ input, label#{el_id} ~ textarea, label#{el_id} + input")
        selectors.append(f"#{el_id} ~ input, #{el_id} ~ textarea, #{el_id} + input")
    return selectors


def resolve_fill_element(element: dict[str, Any] | None) -> dict[str, Any] | None:
    """If a fill landed on a label, retarget selectors at the associated input."""

    if not element or not is_label_element(element):
        return element
    updated = dict(element)
    associated = label_input_selectors(element)
    if associated:
        updated["css"] = associated[0]
        updated["associated_selectors"] = associated
        updated["id"] = element.get("htmlFor") or element.get("for") or None
        updated["tag"] = "input"
        updated["resolved_from_label"] = element.get("id") or element.get("text")
    return updated


def strategies_for_element(element: dict[str, Any] | None, *, include_text: bool = True, for_fill: bool = False) -> list[SelectorStrategy]:
    """Build a priority-ordered selector chain from a captured element."""

    if not element:
        return []
    if for_fill or is_label_element(element):
        element = resolve_fill_element(element) or element
    strategies: list[SelectorStrategy] = []
    seen: set[tuple[str, str]] = set()

    def add(type_: str, value: str, priority: int, **meta: Any) -> None:
        if not value:
            return
        key = (type_, value)
        if key in seen:
            return
        seen.add(key)
        strategies.append(SelectorStrategy(type=type_, value=value, priority=priority, metadata=meta))

    typ = str(element.get("type") or "").lower()
    el_id = element.get("id")
    html_name = element.get("htmlName") or element.get("attrName")
    el_value = element.get("value")
    # Visible radio/checkbox labels beat a hidden input id. Prefer the label text
    # so a later replay can choose a different option (Male vs Female).
    if typ in {"radio", "checkbox"}:
        label_text = str(element.get("text") or element.get("value") or "").strip()
        if label_text:
            add("text_exact", label_text, 8, source="radio_label_text")
            add("xpath", f"//label[normalize-space()='{label_text}']", 8, source="radio_label_xpath")
        if el_id:
            add("css", f"label[for='{el_id}']", 9, source="radio_label")
    if el_id and not str(el_id).lower().endswith("label"):
        add("css", f"#{el_id}", 10, source="id")
    for attr, key in (("data-test", "dataTest"), ("data-testid", "dataTestId"), ("data-cy", "dataCy")):
        val = element.get(key) or element.get(attr)
        if val:
            add("css", f'[{attr}="{val}"]', 11, source=attr)
    if html_name:
        if typ in {"radio", "checkbox"} and el_value:
            add("css", f"input[name='{html_name}'][value='{el_value}']", 12, source="name_value")
        else:
            add("css", f"[name='{html_name}']", 13, source="name")
    for i, sel in enumerate(element.get("associated_selectors") or []):
        add("css", sel, 8 + i, source="label_for")
    aria = element.get("ariaLabel")
    if aria:
        add("aria_label", aria, 20, source="aria")
    placeholder = element.get("placeholder")
    if placeholder:
        add("placeholder", placeholder, 30, source="placeholder")
    role = element.get("role")
    name = element.get("name") or element.get("text")
    if role and name:
        add("role_text", name, 40, source="a11y", role=role)
    if include_text and element.get("text"):
        add("text_exact", element["text"], 50, source="text")
        add("text_fuzzy", element["text"], 80, source="text_fuzzy")
    css = element.get("css")
    if css:
        add("css", css, 60, source="css_path")
    xpath = element.get("xpath")
    if xpath:
        add("xpath", xpath, 70, source="xpath")
    return strategies
