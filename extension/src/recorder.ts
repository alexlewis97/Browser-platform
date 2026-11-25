/**
 * Injected into the builder's tab while Record is on.
 * Captures clicks, typed input, selects, Enter, and reports URL/DOM change.
 */
type RecEvent = {
  type: string;
  ts: number;
  url?: string;
  url_after?: string;
  value?: string | null;
  key?: string;
  text?: string | null;
  changed?: boolean;
  element?: Record<string, string | null | undefined> | null;
};

declare global {
  interface Window {
    __bpPageRecorder?: { drain: () => RecEvent[]; snapshot: () => RecEvent[] };
  }
}

(() => {
  if (window.__bpPageRecorder) return;
  const escape = window.CSS && CSS.escape ? CSS.escape : (s: string) => String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&");

  function cssPath(node: Element | null): string {
    if (!node || node.nodeType !== 1) return "";
    if ((node as HTMLElement).id) return `#${escape((node as HTMLElement).id)}`;
    const parts: string[] = [];
    let cur: Element | null = node;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement && parts.length < 6) {
      if ((cur as HTMLElement).id) {
        parts.unshift(`#${escape((cur as HTMLElement).id)}`);
        break;
      }
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

  function xpath(node: Element | null): string {
    if (!node || node.nodeType !== 1) return "";
    if ((node as HTMLElement).id) return `//*[@id="${(node as HTMLElement).id}"]`;
    const parts: string[] = [];
    let cur: Element | null = node;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement) {
      let ix = 1;
      let sib = cur.previousElementSibling;
      while (sib) {
        if (sib.tagName === cur.tagName) ix += 1;
        sib = sib.previousElementSibling;
      }
      parts.unshift(`${cur.tagName.toLowerCase()}[${ix}]`);
      cur = cur.parentElement;
    }
    return "//" + parts.join("/");
  }

  function describe(el: Element | null) {
    if (!el || el.nodeType !== 1) return null;
    const html = el as HTMLElement;
    const text = ((html.innerText || el.textContent || "").replace(/\s+/g, " ").trim()).slice(0, 160);
    const tag = el.tagName.toLowerCase();
    const role =
      el.getAttribute("role") ||
      ({ a: "link", button: "button", input: "textbox", select: "combobox", textarea: "textbox" } as Record<string, string>)[tag] ||
      tag;
    const name = el.getAttribute("aria-label") || el.getAttribute("name") || el.getAttribute("placeholder") || text;
    const value = "value" in html ? String((html as HTMLInputElement).value ?? "") : null;
    return {
      tag,
      id: html.id || null,
      role,
      name: (name || "").slice(0, 160),
      text,
      value,
      placeholder: el.getAttribute("placeholder"),
      ariaLabel: el.getAttribute("aria-label"),
      type: el.getAttribute("type"),
      href: el.getAttribute("href"),
      css: cssPath(el),
      xpath: xpath(el),
    };
  }

  const events: RecEvent[] = [];
  const lastValues = new Map<string, string>();
  let pending = 0;

  function emit(rec: RecEvent) {
    events.push(rec);
    try {
      chrome.runtime.sendMessage({ kind: "bp-record-event", event: rec }).catch(() => undefined);
    } catch {
      /* service worker may be asleep; drain() still has the event */
    }
  }

  function fieldKey(el: Element | null): string {
    if (!el) return "";
    if ((el as HTMLElement).id) return "#" + (el as HTMLElement).id;
    return cssPath(el) || xpath(el) || "";
  }

  function skipField(el: Element | null): boolean {
    const typ = ((el && el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
    return ["hidden", "submit", "button", "image", "file", "reset", "checkbox", "radio"].includes(typ);
  }

  function fieldValue(el: Element): string {
    const tag = (el.tagName || "").toLowerCase();
    const typ = (el.getAttribute("type") || "").toLowerCase();
    const input = el as HTMLInputElement;
    if (typ === "checkbox" || typ === "radio") return input.checked ? input.value || "on" : "";
    if (tag === "select") {
      const sel = el as HTMLSelectElement;
      const opt = sel.options && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex] : null;
      return opt ? opt.text || sel.value : sel.value || "";
    }
    return input.value != null ? String(input.value) : "";
  }

  function noteField(el: Element | null, reason: string) {
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
    const type = tag === "select" ? "select_change" : "input";
    emit({ type, ts: Date.now(), url: location.href, value, text: describe(el)?.text, element: describe(el), changed: true });
    void reason;
  }

  function snapshotFields() {
    document.querySelectorAll("input, textarea, select").forEach((el) => noteField(el, "snapshot"));
  }

  function fingerprint(): string {
    return `${location.href}|${document.title}|${(document.body?.innerText || "").length}`;
  }

  document.addEventListener(
    "click",
    (e) => {
      const el = e.target as Element | null;
      if (!el) return;
      snapshotFields();
      const before = fingerprint();
      const beforeUrl = location.href;
      const rec: RecEvent = { type: "click", ts: Date.now(), url: beforeUrl, text: describe(el)?.text, element: describe(el) };
      pending += 1;
      window.setTimeout(() => {
        rec.url_after = location.href;
        rec.changed = fingerprint() !== before;
        emit(rec);
        pending -= 1;
      }, 350);
    },
    true,
  );

  document.addEventListener(
    "input",
    (e) => {
      const el = e.target as Element | null;
      if (!el) return;
      const tag = (el.tagName || "").toLowerCase();
      if (tag === "select") return;
      lastValues.set(fieldKey(el), fieldValue(el));
      emit({
        type: "input",
        ts: Date.now(),
        url: location.href,
        value: (el as HTMLInputElement).value,
        text: describe(el)?.text,
        element: describe(el),
        changed: true,
      });
    },
    true,
  );

  document.addEventListener(
    "change",
    (e) => {
      const el = e.target as Element | null;
      if (!el) return;
      const tag = (el.tagName || "").toLowerCase();
      lastValues.set(fieldKey(el), fieldValue(el));
      if (tag === "select") {
        const sel = el as HTMLSelectElement;
        const opt = sel.options[sel.selectedIndex];
        emit({
          type: "select_change",
          ts: Date.now(),
          url: location.href,
          value: opt ? opt.text : sel.value,
          text: opt ? opt.text : sel.value,
          element: describe(el),
          changed: true,
        });
      } else {
        emit({
          type: "input",
          ts: Date.now(),
          url: location.href,
          value: (el as HTMLInputElement).value,
          text: describe(el)?.text,
          element: describe(el),
          changed: true,
        });
      }
    },
    true,
  );

  document.addEventListener(
    "keydown",
    (e) => {
      if (e.key === "Enter" || e.key === "Escape") {
        snapshotFields();
        emit({ type: "key_press", ts: Date.now(), url: location.href, key: e.key, element: describe(e.target as Element), changed: true });
      }
    },
    true,
  );

  window.__bpPageRecorder = {
    drain() {
      snapshotFields();
      const out = events.slice();
      events.length = 0;
      return out;
    },
    snapshot() {
      snapshotFields();
      return events.slice();
    },
  };

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.kind !== "bp-record-drain") return;
    const wait = pending > 0 ? 400 : 0;
    window.setTimeout(() => {
      snapshotFields();
      const out = events.slice();
      events.length = 0;
      sendResponse({ ok: true, events: out });
    }, wait);
    return true;
  });
})();
