import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");

describe("run-site extension contract", () => {
  it("allows the run site in the manifest and web-bridge", () => {
    const manifest = JSON.parse(readFileSync(path.join(root, "manifest.json"), "utf8")) as {
      version: string;
      content_scripts: { matches: string[] }[];
      externally_connectable: { matches: string[] };
    };
    const origin = "https://browser-platform-run.vercel.app/*";
    assert.equal(manifest.version, "1.4.0");
    assert.ok(manifest.content_scripts[0].matches.includes(origin));
    assert.ok(manifest.externally_connectable.matches.includes(origin));

    const bridge = readFileSync(path.join(root, "src/web-bridge.ts"), "utf8");
    assert.ok(bridge.includes("https://browser-platform-run.vercel.app"));
    assert.ok(bridge.includes('"bp-run-request"'));
    assert.ok(bridge.includes('direction: "progress"'));

    const background = readFileSync(path.join(root, "src/background.ts"), "utf8");
    assert.ok(background.includes("confirmRunRequest"));
    assert.ok(background.includes('kind === "bp-run-request"'));
    assert.ok(background.includes("bp-confirm-decision"));
  });
});
