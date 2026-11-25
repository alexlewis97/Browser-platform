import assert from "node:assert/strict";
import { test } from "node:test";
import { normalizeText } from "../src/lib/normalize.ts";
import { scoreCandidates, scoreText } from "../src/lib/score.ts";
import type { Candidate } from "../src/lib/types.ts";

const ROSTER = [
  "Alex Lewis",
  "John Smith",
  "John Smyth",
  "José García",
  "מריה לוי",
  "יונתן כהן",
  "Anna Cohen",
  "Anne Cohen",
  "Wei Zhang",
  "Sarah O'Connor",
];

function candidates(): Candidate[] {
  return ROSTER.map((name, i) => ({ id: `c${i}`, text: name, role: "row", selector: `tr[data-name="${name}"]` }));
}

test("normalize diacritics, Hebrew niqqud, and name order", () => {
  assert.equal(normalizeText("José García"), "jose garcia");
  assert.equal(normalizeText("יוֹנָתָן כֹּהֵן"), normalizeText("יונתן כהן"));
  assert.equal(scoreText("Lewis, Alex", "Alex Lewis"), 100);
  assert.equal(scoreText("כהן יונתן", "יונתן כהן"), 100);
  assert.equal(scoreText("Zhang, Wei", "Wei Zhang"), 100);
});

test("score accepts typos and flags close names", () => {
  const accepted = scoreCandidates("Jon Smith", candidates(), { threshold: 90, margin: 8 });
  assert.equal(accepted.accepted, true);
  assert.equal(accepted.winner?.text, "John Smith");

  const ambiguous = scoreCandidates("Ann Cohen", candidates(), { threshold: 90, margin: 8 });
  assert.equal(ambiguous.accepted, false);
  assert.equal(ambiguous.reason, "ambiguous_margin");
  assert.deepEqual(new Set(ambiguous.ranked.slice(0, 2).map((c) => c.text)), new Set(["Anna Cohen", "Anne Cohen"]));
});
