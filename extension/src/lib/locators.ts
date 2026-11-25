/** Resolve a selector strategy against the current document (content-script DOM). */

import type { SelectorStrategy } from "./types.ts";

const ROLE_ALIASES: Record<string, string> = {
  a: "link",
  link: "link",
  button: "button",
  btn: "button",
  heading: "heading",
  h1: "heading",
  h2: "heading",
  textbox: "textbox",
  input: "textbox",
  searchbox: "searchbox",
  checkbox: "checkbox",
  radio: "radio",
  combobox: "combobox",
  listbox: "listbox",
  option: "option",
  tab: "tab",
  menuitem: "menuitem",
};

function cssEscape(value: string): string {
  if (typeof CSS !== "undefined" && CSS.escape) return CSS.escape(value);
  return String(value).replace(/[^a-zA-Z0-9_\-]/g, "\\$&");
}

function visible(el: Element): boolean {
  const html = el as HTMLElement;
  if (!html.getBoundingClientRect) return true;
  const rect = html.getBoundingClientRect();
  const style = window.getComputedStyle(html);
  if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
  return rect.width > 0 || rect.height > 0 || html.getClientRects().length > 0;
}

function firstVisible(nodes: Element[]): Element | null {
  const list = nodes.filter((n) => n.nodeType === 1);
  return list.find((n) => visible(n)) || list[0] || null;
}

function allBySelector(sel: string): Element[] {
  try {
    return Array.from(document.querySelectorAll(sel));
  } catch {
    return [];
  }
}

function allByXpath(expr: string): Element[] {
  const value = expr.startsWith("xpath=") ? expr.slice(6) : expr;
  const out: Element[] = [];
  try {
    const snap = document.evaluate(value, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    for (let i = 0; i < snap.snapshotLength; i++) {
      const n = snap.snapshotItem(i);
      if (n instanceof Element) out.push(n);
    }
  } catch {
    /* invalid xpath */
  }
  return out;
}

function textOf(el: Element): string {
  return ((el as HTMLElement).innerText || el.textContent || "").replace(/\s+/g, " ").trim();
}

function allByText(value: string, exact: boolean): Element[] {
  const needle = exact ? value.trim() : value.trim().toLowerCase();
  if (!needle) return [];
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_ELEMENT);
  const hits: Element[] = [];
  let node = walker.currentNode as Element | null;
  while (node) {
    const el = node;
    const t = textOf(el);
    const ok = exact ? t === needle : t.toLowerCase().includes(needle.toLowerCase());
    if (ok) hits.push(el);
    node = walker.nextNode() as Element | null;
  }
  // Prefer the deepest (most specific) match
  return hits.sort((a, b) => (a.contains(b) ? 1 : b.contains(a) ? -1 : 0));
}

function accessibleName(el: Element): string {
  const lab = (el as HTMLElement).getAttribute("aria-label")
    || (el as HTMLInputElement).placeholder
    || (el as HTMLElement).getAttribute("title")
    || "";
  if (lab.trim()) return lab.trim();
  const id = el.getAttribute("id");
  if (id) {
    const via = document.querySelector(`label[for="${cssEscape(id)}"]`);
    if (via) return textOf(via);
  }
  const wrapped = el.closest("label");
  if (wrapped) return textOf(wrapped);
  return textOf(el);
}

function allByRole(role: string, name: string): Element[] {
  const mapped = ROLE_ALIASES[role] || role;
  const implicit: Record<string, string> = {
    link: "a[href]",
    button: "button, [type=submit], [type=button], input[type=submit]",
    heading: "h1,h2,h3,h4,h5,h6,[role=heading]",
    textbox: "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio]), textarea, [role=textbox]",
    searchbox: "input[type=search], [role=searchbox]",
    checkbox: "input[type=checkbox], [role=checkbox]",
    radio: "input[type=radio], [role=radio]",
    combobox: "select, [role=combobox]",
    option: "option, [role=option]",
  };
  const sel = `[role="${cssEscape(mapped)}"]` + (implicit[mapped] ? `, ${implicit[mapped]}` : "");
  const want = name.trim().toLowerCase();
  return allBySelector(sel).filter((el) => {
    const n = accessibleName(el).toLowerCase();
    return !want || n === want || n.includes(want);
  });
}

export function elementsFor(strategy: SelectorStrategy): Element[] {
  const typ = (strategy.type || "").toLowerCase();
  const value = strategy.value || "";
  const meta = strategy.metadata || {};
  let role = String(meta.role || meta.tag || "").toLowerCase();
  role = ROLE_ALIASES[role] || role;

  if (typ === "cached") {
    const inner = String(meta.original_type || "css");
    return elementsFor({ type: inner, value, metadata: meta, priority: 1 });
  }
  if (typ === "id" || typ === "css_id") {
    const sel = value.startsWith("#") ? value : `#${value.replace(/^#/, "")}`;
    return allBySelector(sel);
  }
  if (["css", "cssselector", "css_selector", "css_attr", "css_class"].includes(typ)) {
    return allBySelector(value);
  }
  if (typ === "xpath") return allByXpath(value);
  if (typ === "text_exact" || typ === "text") return allByText(value, true);
  if (typ === "text_fuzzy") return allByText(value, false);
  if (typ === "role_text" || typ === "role") return allByRole(role || "button", value);
  if (typ === "aria_label" || typ === "aria") return allBySelector(`[aria-label="${cssEscape(value)}"]`);
  if (typ === "placeholder") return allBySelector(`[placeholder="${cssEscape(value)}"]`);
  if (typ === "title") return allBySelector(`[title="${cssEscape(value)}"]`);
  if (typ === "alt_text" || typ === "alt") return allBySelector(`[alt="${cssEscape(value)}"]`);
  if (typ === "name" || typ === "attr_name") return allBySelector(`[name="${cssEscape(value)}"]`);
  if (typ === "label") {
    return allBySelector("input, textarea, select").filter((el) => accessibleName(el).toLowerCase().includes(value.trim().toLowerCase()));
  }
  if (typ === "test_id" || typ === "data_testid") return allBySelector(`[data-testid="${cssEscape(value)}"]`);
  if (value) return allBySelector(value);
  return [];
}

export function resolveElement(strategy: SelectorStrategy): Element | null {
  return firstVisible(elementsFor(strategy));
}

export function setInputValue(el: Element, value: string): void {
  const input = el as HTMLInputElement;
  const proto = input instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, "value");
  if (desc?.set) desc.set.call(input, value);
  else input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

export function selectOption(el: Element, label: string, optionValue?: string | null): void {
  let select = el as HTMLSelectElement;
  if (el.tagName.toLowerCase() === "option") {
    select = (el.closest("select") as HTMLSelectElement) || select;
  }
  if (select && select.tagName.toLowerCase() === "select") {
    const want = (optionValue ?? label).trim();
    const opt = Array.from(select.options).find((o) => o.value === want || o.text.trim() === want || o.text.trim().includes(want));
    if (opt) {
      select.value = opt.value;
      select.dispatchEvent(new Event("input", { bubbles: true }));
      select.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
  }
  (el as HTMLElement).click();
}

export function pressKey(el: Element, key: string): void {
  (el as HTMLElement).focus();
  const ev = { key, code: key.length === 1 ? `Key${key.toUpperCase()}` : key, bubbles: true, cancelable: true };
  el.dispatchEvent(new KeyboardEvent("keydown", ev));
  el.dispatchEvent(new KeyboardEvent("keypress", ev));
  el.dispatchEvent(new KeyboardEvent("keyup", ev));
  if (key === "Enter" && el instanceof HTMLInputElement) {
    el.form?.requestSubmit();
  }
}

export function extractContent(step: { extractionGoal?: string; goal?: string; description?: string; cssSelector?: string }): string {
  const goal = (step.extractionGoal || step.goal || step.description || "").toLowerCase();
  if (step.cssSelector) {
    const el = document.querySelector(step.cssSelector) as HTMLElement | null;
    if (el && el.innerText.trim()) return el.innerText.trim();
  }
  if (goal.includes("heading") || goal.includes("title") || goal.includes("h1")) {
    const h1 = document.querySelector("h1") as HTMLElement | null;
    if (h1 && h1.innerText.trim()) return h1.innerText.trim();
    return document.title.trim();
  }
  if (goal.includes("paragraph") || goal.includes("article")) {
    const paras = document.querySelectorAll("main p, article p, #mw-content-text p, p");
    for (const p of Array.from(paras).slice(0, 8)) {
      const text = (p as HTMLElement).innerText.trim();
      if (text) return text.slice(0, 4000);
    }
    const main = document.querySelector("main, article") as HTMLElement | null;
    if (main && main.innerText.trim()) return main.innerText.trim().slice(0, 4000);
  }
  const main = document.querySelector("main, article") as HTMLElement | null;
  if (main && main.innerText.trim()) return main.innerText.trim().slice(0, 4000);
  return (document.body?.innerText || "").trim().slice(0, 4000);
}

export function pageInfo(): { url: string; title: string; body: string; headings: string[]; wikimediaError: boolean } {
  const title = document.title || "";
  const body = (document.body?.innerText || "").slice(0, 800);
  const headings = Array.from(document.querySelectorAll("h1, h2, [role=heading]")).map((h) => (h as HTMLElement).innerText.trim());
  return {
    url: location.href,
    title,
    body,
    headings,
    wikimediaError: title.includes("Wikimedia Error") || body.includes("Too many requests"),
  };
}

const DEFAULT_CANDIDATE_SELECTOR = [
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
].join(",");

function shortPath(el: Element): string {
  if (el.id) return `#${cssEscape(el.id)}`;
  const dataName = el.getAttribute("data-name");
  if (dataName) return `${el.tagName.toLowerCase()}[data-name="${cssEscape(dataName)}"]`;
  const value = el.getAttribute("value");
  if (el.tagName === "OPTION" && value != null) {
    const sel = el.closest("select");
    const root = sel ? shortPath(sel) : "select";
    return `${root} option[value="${cssEscape(value)}"]`;
  }
  const parts: string[] = [];
  let cur: Element | null = el;
  while (cur && cur !== document.documentElement && parts.length < 6) {
    if (cur.id) {
      parts.unshift(`#${cssEscape(cur.id)}`);
      break;
    }
    let part = cur.tagName.toLowerCase();
    const parentEl: Element | null = cur.parentElement;
    if (parentEl) {
      const same = Array.from(parentEl.children).filter((c) => c.tagName === cur!.tagName);
      if (same.length > 1) part += `:nth-of-type(${same.indexOf(cur) + 1})`;
    }
    parts.unshift(part);
    cur = parentEl;
  }
  return parts.join(" > ");
}

export function collectCandidates(opts: { selector?: string; limit?: number; containerHint?: string; typ?: string }): Array<{
  text: string;
  role: string;
  tag: string;
  value: string | null;
  selector: string;
  disabled: boolean;
}> {
  let selector = opts.selector || DEFAULT_CANDIDATE_SELECTOR;
  if (opts.typ === "select_change") selector = "option, [role='option']";
  let root: ParentNode = document;
  if (opts.containerHint) {
    try {
      const hit = document.querySelector(opts.containerHint);
      if (hit) root = hit;
    } catch {
      /* ignore */
    }
  }
  const nodes = Array.from(root.querySelectorAll(selector)).slice(0, opts.limit ?? 80);
  return nodes
    .map((el) => {
      const dataName = el.getAttribute("data-name");
      let text = "";
      if (dataName && dataName.trim()) text = dataName.trim();
      else if (el.tagName === "TR") {
        const cell = el.querySelector("th,td") as HTMLElement | null;
        text = (cell?.innerText || "").replace(/\s+/g, " ").trim();
      } else {
        text = ((el as HTMLElement).innerText || el.textContent || el.getAttribute("aria-label") || el.getAttribute("label") || (el as HTMLInputElement).value || "")
          .replace(/\s+/g, " ")
          .trim();
      }
      return {
        text,
        role: el.getAttribute("role") || el.tagName.toLowerCase(),
        tag: el.tagName.toLowerCase(),
        value: (el as HTMLInputElement).value != null ? String((el as HTMLInputElement).value) : null,
        selector: shortPath(el),
        disabled: !!(el as HTMLInputElement).disabled || el.getAttribute("aria-disabled") === "true",
      };
    })
    .filter((c) => c.text);
}
