/** Normalize labels for fuzzy matching: diacritics, Hebrew niqqud, RTL marks, name order. */

const HEBREW_LO = 0x0591;
const HEBREW_HI = 0x05c7;
const BIDI = new Set([0x200e, 0x200f, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e, 0x2066, 0x2067, 0x2068, 0x2069]);
const PUNCT_TO_SPACE = /[,;|/\-_·•]+/g;
const WS = /\s+/g;
const APOS = /['’`´]/g;

function isHebrewPoint(code: number): boolean {
  return code >= HEBREW_LO && code <= HEBREW_HI;
}

function isArabicMark(code: number): boolean {
  return (code >= 0x064b && code <= 0x065f) || code === 0x0670 || (code >= 0x06d6 && code <= 0x06ec);
}

export function stripMarks(text: string): string {
  const decomposed = text.normalize("NFKD");
  let out = "";
  for (const ch of decomposed) {
    const code = ch.codePointAt(0) ?? 0;
    if (/\p{M}/u.test(ch)) continue;
    if (isHebrewPoint(code) || isArabicMark(code) || BIDI.has(code)) continue;
    out += ch;
  }
  return out;
}

export function normalizeText(text: string | null | undefined): string {
  if (!text) return "";
  let value = String(text).normalize("NFKC");
  value = stripMarks(value);
  value = value.replace(APOS, "");
  value = value.replace(PUNCT_TO_SPACE, " ");
  value = value.replace(WS, " ").trim().toLocaleLowerCase();
  // casefold-ish: JS toLocaleLowerCase covers the Latin/Hebrew cases we care about.
  return value;
}

export function nameVariants(text: string | null | undefined): Set<string> {
  const raw = String(text || "").trim();
  const variants = new Set<string>();
  const norm = normalizeText(raw);
  if (norm) variants.add(norm);
  if (raw.includes(",")) {
    const parts = raw.split(",").map((p) => p.trim()).filter(Boolean);
    if (parts.length === 2) {
      const swapped = normalizeText(`${parts[1]} ${parts[0]}`);
      if (swapped) variants.add(swapped);
    }
  }
  const tokens = norm.split(" ").filter(Boolean);
  if (tokens.length >= 2) {
    variants.add([...tokens].reverse().join(" "));
    variants.add(`${tokens[tokens.length - 1]} ${tokens.slice(0, -1).join(" ")}`);
    variants.add(`${tokens.slice(1).join(" ")} ${tokens[0]}`);
    variants.add([...tokens].sort().join(" "));
  }
  return variants.size ? variants : new Set([""]);
}
