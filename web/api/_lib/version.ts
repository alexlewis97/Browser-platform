export function bumpVersion(version: string): string {
  const text = String(version || "").trim() || "1";
  const parts = text.split(".");
  if (!parts.every((part) => /^\d+$/.test(part))) return `${text}+1`;
  const nums = parts.map((part) => Number(part));
  nums[nums.length - 1] += 1;
  return nums.join(".");
}

/** Compare dotted numeric versions. Non-numeric falls back to string compare. */
export function versionGreater(a: string, b: string): boolean {
  const pa = String(a || "").split(".");
  const pb = String(b || "").split(".");
  if (!pa.every((p) => /^\d+$/.test(p)) || !pb.every((p) => /^\d+$/.test(p))) return a > b;
  const n = Math.max(pa.length, pb.length);
  for (let i = 0; i < n; i++) {
    const da = Number(pa[i] || 0);
    const db = Number(pb[i] || 0);
    if (da !== db) return da > db;
  }
  return false;
}
