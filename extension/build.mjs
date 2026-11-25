import * as esbuild from "esbuild";
import { cpSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const outdir = join(root, "dist");
mkdirSync(outdir, { recursive: true });

await esbuild.build({
  entryPoints: {
    background: join(root, "src/background.ts"),
    content: join(root, "src/content.ts"),
    recorder: join(root, "src/recorder.ts"),
    "web-bridge": join(root, "src/web-bridge.ts"),
    sidepanel: join(root, "src/sidepanel.ts"),
  },
  bundle: true,
  format: "esm",
  target: "chrome114",
  outdir,
  sourcemap: false,
  legalComments: "none",
  logLevel: "info",
});

cpSync(join(root, "src/sidepanel.html"), join(outdir, "sidepanel.html"));
cpSync(join(root, "src/sidepanel.css"), join(outdir, "sidepanel.css"));
writeFileSync(join(outdir, "manifest.json"), readFileSync(join(root, "manifest.json")));
console.log("wrote", outdir);
