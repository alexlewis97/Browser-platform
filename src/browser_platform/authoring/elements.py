"""Turn a recorded DOM element description into workflow-use selectorStrategies."""

from __future__ import annotations

from typing import Any

from browser_platform.models.workflow import SelectorStrategy

_JS_DESCRIBE = r"""
(el) => {
  if (!el || el.nodeType !== 1) return null;
  const escape = (window.CSS && CSS.escape) ? CSS.escape : (s) => String(s).replace(/[^a-zA-Z0-9_\-]/g, '\\$&');
  function cssPath(node) {
    if (node.id) return `#${escape(node.id)}`;
    const parts = [];
    let cur = node;
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
    css: cssPath(el),
    xpath: xpath(el),
  };
}
"""

RECORDER_JS = r"""
(() => {
  if (window.__bpRecorder) return;
  const escape = (window.CSS && CSS.escape) ? CSS.escape : (s) => String(s).replace(/[^a-zA-Z0-9_\-]/g, '\\$&');
  function cssPath(node) {
    if (!node || node.nodeType !== 1) return "";
    if (node.id) return `#${escape(node.id)}`;
    const parts = [];
    let cur = node;
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
  function describe(el) {
    if (!el || el.nodeType !== 1) return null;
    const text = ((el.innerText || el.textContent || "").replace(/\s+/g, " ").trim()).slice(0, 160);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role") || ({ a: "link", button: "button", input: "textbox", select: "combobox", textarea: "textbox" }[tag] || tag);
    const name = el.getAttribute("aria-label") || el.getAttribute("name") || el.getAttribute("placeholder") || text;
    return {
      tag, id: el.id || null, role, name: (name || "").slice(0, 160), text,
      value: el.value != null ? String(el.value) : null,
      placeholder: el.getAttribute("placeholder"),
      ariaLabel: el.getAttribute("aria-label"),
      type: el.getAttribute("type"),
      href: el.getAttribute("href"),
      css: cssPath(el),
      xpath: xpath(el),
    };
  }
  const events = [];
  const lastValues = new Map();
  function push(type, el, extra) {
    const rec = { type, ts: Date.now(), url: location.href, element: describe(el), ...(extra || {}) };
    events.push(rec);
  }
  function fieldKey(el) {
    if (!el) return "";
    if (el.id) return "#" + el.id;
    return cssPath(el) || xpath(el) || "";
  }
  function skipField(el) {
    const typ = ((el && el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
    // Checkboxes/radios include site chrome (e.g. Wikipedia's menu dropdown). They are not typed values.
    return ["hidden", "submit", "button", "image", "file", "reset", "checkbox", "radio"].includes(typ);
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
    if (!el || el.nodeType !== 1 || skipField(el)) return;
    const tag = (el.tagName || "").toLowerCase();
    if (!["input", "textarea", "select"].includes(tag)) return;
    const key = fieldKey(el);
    if (!key) return;
    const value = fieldValue(el);
    if (lastValues.get(key) === value) return;
    const had = lastValues.has(key);
    lastValues.set(key, value);
    if (!had && value === "") return;
    if (tag === "select") push("select_change", el, { value, selected: el.value, source: reason || "diff" });
    else push("input", el, { value, source: reason || "diff" });
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
    if (e.target) {
      snapshotFields();
      push("click", e.target);
    }
  }, true);
  document.addEventListener("input", (e) => {
    const el = e.target;
    if (!el) return;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "select") return;
    lastValues.set(fieldKey(el), fieldValue(el));
    push("input", el, { value: el.value, source: "input" });
  }, true);
  document.addEventListener("change", (e) => {
    const el = e.target;
    if (!el) return;
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
  window.__bpRecorder = {
    events,
    drain() { snapshotFields(); const out = events.slice(); events.length = 0; return out; },
    snapshot() { return events.slice(); },
    snapshotFields,
  };
})();
"""


def strategies_for_element(element: dict[str, Any] | None, *, include_text: bool = True) -> list[SelectorStrategy]:
    """Build a priority-ordered selector chain from a captured element."""

    if not element:
        return []
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

    el_id = element.get("id")
    if el_id:
        add("css", f"#{el_id}", 10, source="id")
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
