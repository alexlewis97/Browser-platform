import assert from "node:assert/strict";
import { test } from "node:test";
import { interpolate, placeholdersIn } from "../src/lib/interpolate.ts";
import { buildSelectorPlan } from "../src/lib/selectors.ts";
import { deriveInputs, missingRequired, prettyLabel } from "../src/lib/inputs.ts";
import type { WorkflowDefinition } from "../src/lib/types.ts";

test("interpolate {var} and {{var}}", () => {
  assert.equal(interpolate("hello {term}", { term: "Ada" }), "hello Ada");
  assert.equal(interpolate("hello {{term}}", { term: "Ada" }), "hello Ada");
  assert.equal(interpolate("hello {missing}", { term: "Ada" }), "hello {missing}");
});

test("pretty labels and required inputs from schema plus placeholders", () => {
  assert.equal(prettyLabel("term"), "Search term");
  const def: WorkflowDefinition = {
    name: "attendance_mark",
    steps: [{ type: "navigation", url: "{url}" }, { type: "click", target_text: "{name}" }, { type: "extract", extractionGoal: "result" }],
    input_schema: [
      { name: "name", type: "string", required: true },
      { name: "status", type: "string", required: true, default: "Present" },
    ],
  };
  const inputs = deriveInputs(def);
  const names = inputs.map((i) => i.name).sort();
  assert.deepEqual(names, ["name", "status", "url"]);
  assert.equal(inputs.find((i) => i.name === "url")?.required, true);
  assert.deepEqual(missingRequired(inputs, { name: "Alex", status: "Present" }), ["url"]);
  assert.deepEqual(missingRequired(inputs, { name: "Alex", status: "Present", url: "http://x" }), []);
});

test("selector plan is cached, then strategies by priority, then legacy, then heuristics", () => {
  const plan = buildSelectorPlan({
    type: "click",
    cssSelector: "#legacy",
    target_text: "Save",
    description: 'Click "Go now"',
    cachedSelector: { type: "css", value: "#cached", priority: 0 },
    selectorStrategies: [
      { type: "css", value: "#b", priority: 20 },
      { type: "css", value: "#a", priority: 10 },
    ],
  });
  const values = plan.map((a) => `${a.kind}:${a.strategy.type}:${a.strategy.value}`);
  assert.equal(values[0], "cached:css:#cached");
  assert.equal(values[1], "selector:css:#a");
  assert.equal(values[2], "selector:css:#b");
  assert.ok(values.includes("selector:css:#legacy"));
  assert.ok(values.includes("selector:text_exact:Save"));
  assert.ok(values.includes("heuristic:text_fuzzy:Save"));
  assert.ok(values.includes("heuristic:text_exact:Go now"));
});

import { interpolateUrl } from "../src/lib/interpolate.ts";

test("interpolate formats {var|slug} and {var|head}", () => {
  assert.equal(interpolate("https://www.bbc.com/sport/football/teams/{team|slug}/scores-fixtures", { team: "Chelsea" }), "https://www.bbc.com/sport/football/teams/chelsea/scores-fixtures");
  assert.equal(interpolate("/teams/{team|head}/scores-fixtures", { team: "England men" }), "/teams/england/scores-fixtures");
});

test("interpolateUrl slugs raw path values and leaves wikipedia titles", () => {
  assert.equal(interpolateUrl("https://www.bbc.com/sport/football/teams/{team}/scores-fixtures", { team: "Chelsea" }), "https://www.bbc.com/sport/football/teams/chelsea/scores-fixtures");
  assert.equal(interpolateUrl("https://en.wikipedia.org/wiki/{term}", { term: "Ada Lovelace" }), "https://en.wikipedia.org/wiki/Ada Lovelace");
});
