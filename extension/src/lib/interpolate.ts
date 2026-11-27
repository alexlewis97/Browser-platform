const BRACE = /\{\{?\s*([a-zA-Z_][\w]*)(?:\|([a-zA-Z0-9_]+(?::[^{}|]*)?))?\s*\}?\}/g;

/** A list value joined for one place: {name|join:+} picks the separator, otherwise sep. */
function joinList(values: unknown[], fmt: string | undefined, sep: string): string {
  let itemFmt = fmt;
  if (fmt && (fmt === "join" || fmt.startsWith("join:"))) {
    sep = fmt.includes(":") ? fmt.slice(fmt.indexOf(":") + 1) : sep;
    itemFmt = undefined;
  }
  return values.filter((v) => v != null && String(v).trim()).map((v) => formatValue(String(v), itemFmt)).join(sep);
}

export function pathSlug(value: string): string {
  return String(value || "")
    .trim()
    .replace(/\s+/g, "-")
    .toLowerCase();
}

export function formatValue(value: string, fmt?: string | null): string {
  const text = String(value || "");
  if (!fmt) return text;
  if (fmt === "slug") return pathSlug(text);
  if (fmt === "slug_" || fmt === "underscore") return text.trim().replace(/\s+/g, "_").toLowerCase();
  if (fmt === "head") {
    const tokens = text.trim().split(/\W+/).filter(Boolean);
    return (tokens[0] || text).toLowerCase();
  }
  return text;
}

export function interpolate(value: unknown, variables?: Record<string, unknown> | null, listSep = ", "): unknown {
  if (!variables || typeof value !== "string") return value;
  return value.replace(BRACE, (match, key: string, fmt?: string) => {
    if (Object.prototype.hasOwnProperty.call(variables, key) && variables[key] != null) {
      const found = variables[key];
      if (Array.isArray(found)) return joinList(found, fmt, listSep);
      if (fmt && (fmt === "join" || fmt.startsWith("join:"))) fmt = undefined;
      return formatValue(String(found), fmt);
    }
    return match;
  });
}

export function interpolateUrl(url: unknown, variables?: Record<string, unknown> | null): unknown {
  if (typeof url !== "string") return interpolate(url, variables);
  let updated = interpolate(url, variables, ",");
  if (typeof updated === "string" && updated.includes("?")) {
    // An optional query parameter whose input was not given is dropped, not sent empty.
    const [base, query] = [updated.slice(0, updated.indexOf("?")), updated.slice(updated.indexOf("?") + 1)];
    const kept = query.split("&").filter((pair) => pair && !/^[^=]+=(\{[^}]*\})?$/.test(pair));
    updated = base + (kept.length ? "?" + kept.join("&") : "");
  }
  if (typeof updated !== "string" || !variables) return updated;
  try {
    const parsed = new URL(updated);
    const host = parsed.hostname.toLowerCase();
    if (host.includes("wikipedia.org") || host.includes("wikimedia.org")) return updated;
    const rawValues = Object.values(variables)
      .filter((v) => v != null && !Array.isArray(v) && String(v).trim())
      .map((v) => String(v).trim());
    const parts = parsed.pathname.split("/");
    let changed = false;
    for (let i = 0; i < parts.length; i += 1) {
      const part = parts[i];
      if (!part) continue;
      const decoded = decodeURIComponent(part);
      for (const text of rawValues) {
        const candidates = new Set([text, text.replaceAll(" ", "_"), text.replaceAll(" ", "-"), text.replaceAll(" ", "+")]);
        const lowered = new Set([...candidates].map((item) => item.toLowerCase()));
        if (candidates.has(decoded) || lowered.has(decoded.toLowerCase())) {
          const slug = pathSlug(text);
          if (parts[i] !== slug) {
            parts[i] = slug;
            changed = true;
          }
          break;
        }
      }
    }
    if (!changed) return updated;
    parsed.pathname = parts.join("/");
    return parsed.toString();
  } catch {
    return updated;
  }
}

export function placeholdersIn(value: unknown, found: string[] = []): string[] {
  if (typeof value === "string") {
    const re = new RegExp(BRACE.source, "g");
    let match: RegExpExecArray | null;
    while ((match = re.exec(value))) {
      if (!found.includes(match[1])) found.push(match[1]);
    }
  } else if (Array.isArray(value)) {
    for (const item of value) placeholdersIn(item, found);
  } else if (value && typeof value === "object") {
    for (const item of Object.values(value as Record<string, unknown>)) placeholdersIn(item, found);
  }
  return found;
}
