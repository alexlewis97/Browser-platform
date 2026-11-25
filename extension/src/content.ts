import {
  collectCandidates,
  extractContent,
  pageInfo,
  pressKey,
  resolveElement,
  selectOption,
  setInputValue,
} from "./lib/locators.ts";
import type { SelectorStrategy } from "./lib/types.ts";

type Action =
  | { op: "ping" }
  | { op: "pageInfo" }
  | { op: "extract"; step: { extractionGoal?: string; goal?: string; description?: string; cssSelector?: string } }
  | { op: "scroll"; scrollX?: number; scrollY?: number }
  | { op: "act"; typ: string; strategy: SelectorStrategy; value?: string; selected?: string; key?: string }
  | { op: "collect"; typ?: string; containerHint?: string; limit?: number }
  | { op: "apply"; typ: string; selector?: string | null; text: string; value?: string | null }
  | { op: "assert"; expected?: { url_includes?: string; title_contains?: string; text_contains?: string; heading?: string } };

type Result = { ok: true; data?: unknown } | { ok: false; error: string };

function handle(action: Action): Result {
  try {
    if (action.op === "ping") return { ok: true, data: { ready: true, url: location.href } };
    if (action.op === "pageInfo") return { ok: true, data: pageInfo() };
    if (action.op === "extract") return { ok: true, data: extractContent(action.step) };
    if (action.op === "scroll") {
      window.scrollBy(action.scrollX || 0, action.scrollY || 0);
      return { ok: true, data: { scrollX: action.scrollX || 0, scrollY: action.scrollY || 0 } };
    }
    if (action.op === "assert") {
      const expected = action.expected;
      if (!expected) return { ok: true };
      const info = pageInfo();
      if (expected.url_includes && !info.url.includes(expected.url_includes)) {
        return { ok: false, error: `url did not include ${JSON.stringify(expected.url_includes)} (was ${info.url})` };
      }
      if (expected.title_contains && !info.title.includes(expected.title_contains)) {
        return { ok: false, error: `title did not contain ${JSON.stringify(expected.title_contains)} (was ${JSON.stringify(info.title)})` };
      }
      const body = document.body?.innerText || "";
      if (expected.text_contains && !body.includes(expected.text_contains)) {
        return { ok: false, error: `page text did not contain ${JSON.stringify(expected.text_contains)}` };
      }
      if (expected.heading && !info.headings.some((h) => h.includes(expected.heading!))) {
        return { ok: false, error: `heading ${JSON.stringify(expected.heading)} not found` };
      }
      return { ok: true };
    }
    if (action.op === "collect") {
      const raw = collectCandidates({ typ: action.typ, containerHint: action.containerHint, limit: action.limit });
      return { ok: true, data: raw.filter((c) => !c.disabled) };
    }
    if (action.op === "apply") {
      let el: Element | null = null;
      if (action.selector) {
        try {
          el = document.querySelector(action.selector);
        } catch {
          el = null;
        }
      }
      if (!el) {
        const nodes = Array.from(document.querySelectorAll("body *")).filter((n) => {
          const t = ((n as HTMLElement).innerText || n.textContent || "").replace(/\s+/g, " ").trim();
          return t === action.text;
        });
        el = nodes.at(-1) || null;
      }
      if (!el) return { ok: false, error: `could not resolve candidate ${action.text}` };
      if (action.typ === "select_change") selectOption(el, action.text, action.value);
      else if (action.typ === "input") setInputValue(el, action.value ?? action.text);
      else (el as HTMLElement).click();
      return { ok: true, data: { selector: action.selector, text: action.text } };
    }
    if (action.op === "act") {
      const el = resolveElement(action.strategy);
      if (!el) return { ok: false, error: `no matching elements for ${action.strategy.type}:${action.strategy.value}` };
      if (action.typ === "click") (el as HTMLElement).click();
      else if (action.typ === "input") {
        if (action.value == null) return { ok: false, error: "input step is missing value" };
        setInputValue(el, String(action.value));
      } else if (action.typ === "select_change") {
        if (action.selected == null) return { ok: false, error: "select_change step is missing selectedText" };
        selectOption(el, String(action.selected));
      } else if (action.typ === "key_press") {
        if (!action.key) return { ok: false, error: "key_press step is missing key" };
        pressKey(el, String(action.key));
      } else {
        return { ok: false, error: `unsupported step type: ${action.typ}` };
      }
      return { ok: true, data: { type: action.strategy.type, value: action.strategy.value } };
    }
    return { ok: false, error: "unknown action" };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.kind !== "bp-action") return;
  sendResponse(handle(message.action as Action));
  return false;
});
