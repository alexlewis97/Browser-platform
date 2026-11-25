# Browser Subagent Platform — Research Report

**Date:** 24 November 2026 (Asia/Jerusalem, IST)  
**Scope:** Research only. No code. Current as of this date from project READMEs, official docs, and OpenRouter model pages.  
**Goal:** A backend that builds browser-automation subagents that are deterministic, token-cheap, and low-latency, falling back to an LLM only when a deterministic step fails. A Chrome extension or web app will connect later. The user stores an OpenRouter API key and model choice in the browser. Backend language is Python (Alex's choice).

---

## 1. Executive summary

The right product shape is not “an LLM that drives Chrome on every step.” It is a **compiler plus a cheap runtime**:

1. **Build once** (AI allowed): turn a recording or a natural-language task into a reusable, schema-validated operation or workflow.
2. **Run many times with zero tokens** on a deterministic executor.
3. **Repair only on failure**, first with selector/heuristic fallbacks, then with a short LLM episode, then **write the fix back** into the cached artifact so the next run is deterministic again.

Three named projects already implement pieces of this, and they compose:

| Piece | Project | Role |
| --- | --- | --- |
| Deterministic tool mint + zero-token runtime | **Pomerado** (`Pomerado/pomerado`, MIT, TypeScript / Node 24) | Best “build a reliable site tool, then run it with no model” system. This is almost certainly what Alex meant by “pomerado.” |
| Deterministic record/replay + (intended) AI fallback | **workflow-use** (`browser-use/workflow-use`, **AGPL-3.0**, Python + Chrome extension) | Closest open recorder UX and semantic replay. Early-stage; self-heal/fallback is currently weak or broken. License is a product constraint. |
| LLM fallback agent on raw CDP | **browser-use 1.0** (`browser-use/browser-use`, MIT, Python) | Mature (~122k stars). Agent writes Python against CDP in a persistent REPL. Can attach to the user’s Chrome. Use only when deterministic replay fails. |

**Recommended stack (logic first, Python backend):**

- **Orchestrator:** Python FastAPI.
- **Deterministic layer:** Pomerado minted TypeScript operations, invoked from Python over stdio MCP (or a thin Node host). Optional HTTP operations when the site can be called without a browser.
- **Recorder / “show once” path:** our own extension (ideas from workflow-use + Playwright MCP + nanobrowser), not a verbatim AGPL fork.
- **LLM fallback:** browser-use 1.0 attached to the same browser, OpenRouter models chosen in the extension.
- **Repair loop:** LLM success must rewrite the cached workflow/tool, which workflow-use lists as unimplemented.
- **Extension:** `chrome.debugger` + WebSocket to the local backend; API key in `chrome.storage.local`, never persisted on the server by default.
- **Default fallback model:** `qwen/qwen3.7-flash` ($0.03 / $0.13 per 1M tokens, vision + tools, 1M context). Cheap text-only alternative: `openai/gpt-oss-20b` ($0.0153 / $0.0765).

The main integration friction is that **stock `pomerado-mcp` will not attach over CDP** (only a Playwright `chromium.connect()` WebSocket). The **core executor used by a custom host can attach over CDP** to a Chromium the host owns. Driving the user’s everyday logged-in Chrome therefore needs either a custom Pomerado host, or Pomerado running in a dedicated browser with saved logins, while browser-use / the extension handle the user’s real Chrome.

---

## 2. browser-use (`github.com/browser-use/browser-use`)

### Maturity and license

- MIT. Python 3.11+. Current library pin in the README: **`browser-use==1.0.8`** (1.0.0 shipped 10 November 2026; 1.0.8 around 22 November 2026).
- Very mature open-source project (~122k GitHub stars as of 23 November 2026, Repocharts).
- Docs warn to **pin exact versions** (`==`, never `>=`); the API moves fast.
- Related: Browser Use Cloud (hosted browsers + agent API, $0.06/browser-hour), TypeScript “Browser Use Pi,” `browser-harness`, CLI and MCP (`browser-use --cli-mcp`).

### Architecture (1.0 — “CDP is all you need”)

1.0 deleted most of the 0.x library. The agent no longer picks one action per step from a DOM dump. It writes Python in **one persistent REPL** that has browser-harness helpers and **raw Chrome DevTools Protocol**. The loop sits on **Pydantic AI**.

```
Agent(task, llm, browser).run()
  -> resolve_model(llm)          # default: bu-fast on Browser Use gateway
  -> Browser.start()             # cloud | local chromium | user Chrome | cdp_url
  -> Pydantic AI loop
       python(code) -> REPL worker -> CDP / ax-tree helpers
  <- AgentHistoryList (result, usage, cost)
```

Claimed vs 0.x on the same models: up to 33 points higher, 2.7–5.2× cheaper, 2–3× faster. `mode='ultrafast'` uses accessibility-tree element IDs (`bu.click(41)` style).

Defaults changed in 1.0: `Browser()` is now a **cloud** browser; `llm=None` uses `bu-fast`. Local Chromium is `Browser('chromium')`. A 0.x pin remains available: `pip install 'browser-use<1'`.

Any LLM is supported (`ChatOpenAI`, Anthropic, Google, or a Pydantic AI model string such as `llm='groq:llama-3.3-70b-versatile'`). OpenRouter can sit behind that.

### CDP / extension support

| Target | How |
| --- | --- |
| Cloud browser | `Browser()` (default) |
| Fresh local Chromium | `Browser('chromium')` |
| **User’s already-open Chrome** | `Browser('chrome')` after enabling `chrome://inspect/#remote-debugging` once and accepting Chrome’s prompt. Goes through a shared browser-harness daemon; **only one** such session at a time. |
| Any CDP endpoint | `Browser(cdp_url=...)` (`http://host:9222` or `ws://...`). Custom connect headers are not supported yet. |

There is **no first-party Chrome extension** that bypasses CDP. An extension that wants to feed this agent must expose a CDP endpoint (for example `chrome.debugger` relayed to a local WebSocket) or have the user enable remote debugging.

Security note from their docs: `Browser('chrome')` can read real cookies and open tabs. Code the model writes is powerful.

### Fit for this platform

browser-use is the **fallback brain**, not the default runtime. It is the right tool when a cached step breaks, and it can drive the same Chrome the extension is attached to. It is the wrong default for repeated tasks: every run still spends tokens and latency.

---

## 3. workflow-use (`github.com/browser-use/workflow-use`)

### Maturity and license

- **AGPL-3.0.** This matters: embedding it in a hosted or distributed product can force source disclosure. Treat it as a reference design, or isolate it behind a clear copyleft boundary. Do not casually vendor the code into an MIT/proprietary backend.
- README: “very early development,” “we don't recommend using this in production,” no release schedule.
- Still actively developed. Latest noted release: **v0.2.11** (19 November 2026). On the order of ~4.2k stars.
- Already ships a **FastAPI** backend, a workflow GUI, a CLI, and a Chrome MV3 recorder extension.

### How record / replay / fallback work

Two ways to get a workflow:

1. **Record once:** `python cli.py create-workflow` after building the extension (`extension && npm install && npm run build`). The MV3 extension captures clicks, inputs, keys, selects, scrolls, and tabs. Events are enriched with **semantic metadata** (visible text, labels, ARIA, placeholders) and, in the recorder path, **rrweb** DOM mutation data. A Python recorder server receives events; `BuilderService` turns them into a structured `.workflow.json` with variables extracted from forms.
2. **Generate from language:** `HealingService.generate_workflow_from_prompt(...)` runs **browser-use once**, then converts the execution history into a parameterized semantic workflow stored under `workflows/storage/`.

Replay:

- Deterministic / no-AI path: `workflow.run_with_no_ai()` and `SemanticWorkflowExecutor`. Mapping is semantic first (visible text / meaning), then CSS / XPath fallbacks, with retries and post-action verification (click caused a transition, input value stuck, etc.).
- Steps can be mixed with explicit agentic steps.
- Cloud browsers are supported (`use_cloud=True`).

**Intended** AI fallback: if a deterministic step throws and `fallback_to_agent` is on and an LLM is supplied, `_fallback_to_agent` builds a prompt from the failed step + full workflow overview and runs a short browser-use agent (`max_steps=5`). The roadmap also wants that agent to **rewrite the workflow file** (true self-heal).

**Current reality (important):** both of those are weak.

- The README still has unchecked items: “Improve LLM fallback when step fails (currently really bad)” and “Self healing, if it fails automatically agent kicks in and updates the workflow file.”
- Issue **#146** (24 January 2026, still the documented failure mode to verify on current main): the fallback block in `service.py` was **commented out**, so logs said “Attempting fallback” and then raised. Even if uncommented, the healer called Playwright APIs (`page.locator()`, `wait_for_load_state()`) on browser-use’s CDP `Page` wrapper, which does not implement them. Workaround given: `run_with_no_ai` and hand-edit YAML/JSON when selectors break.

MCP exposure of workflows is also still on the roadmap (unchecked).

### CDP / extension support

The extension is a **recorder**, not a CDP driver. Replay runs in the Python worker (browser-use / Chromium / cloud), not inside the user’s everyday tab via `chrome.debugger`.

### Fit for this platform

Steal the **product ideas**: show-don’t-prompt recording, semantic selector chains, verification, generate-from-one-agent-run, FastAPI+GUI sketch. Do **not** depend on its self-heal as it exists today, and do **not** pull the repo in without an AGPL decision. The missing piece we must own is: **fallback success writes back a patched workflow**.

---

## 4. Pomerado (`github.com/Pomerado/pomerado`)

This is a real, on-point project — not a typo for Pomodoro or Pomerium. Site: [pomerado.ai](https://pomerado.ai). Docs: [docs.pomerado.ai](https://docs.pomerado.ai). YC F26. TypeScript, Node **24.21 or later (not 25)**. Current license **MIT** (versions ≤ 0.1.2 were AGPL-3.0-only).

### What it is

Pomerado **mints a deterministic MCP/API tool for one task on one website** from a natural-language description. No HAR file or human recording is required. After minting, **runs make no model calls**.

```
explore the site → complete your example → generalize → agent calls the saved tool
```

Three parts, plus a job model:

1. **Minter** — a model agent with a workspace and a real Chromium, driven through **native Playwright**. It writes operation modules under `src/`, executes them live, runs live tests (a read tool’s publication expects at least 70% of counted cases to pass), and handles sign-in / 2FA by asking the user (credentials are not saved). A mint has 20 minutes of active work; waiting for answers does not count. Write mints perform the side-effecting action once while building, then review source without repeating it.
2. **Guardian** — a separate review loop. It judges **each browser action before it runs** and reviews finished source / publication. It never sees the action’s result. Policy is adapted from OpenAI Codex (Apache-2.0). Reviews have tight bounds (turns, model-call timeouts, compaction).
3. **Outcome reviewer** — a third session (docs name **GPT-6 Luna** for this role) that, after a write, judges whether the site actually changed (`done` / `not_done` / `unknown`) from network/commit evidence and readbacks. The minter only retries a write after this reviewer says `not_done`.
4. **Runtime** — a saved integration is a folder of source the owner keeps. It is served as its **own MCP stdio server** (`mcp.mjs` / `pomerado-mcp serve --artifact`). Input and output are schema-validated. Read tools are marked read-only and idempotent. Popup / confirm / file / list-cursor / same-site rules are all in the runtime, not left to the model.

### Artifact shape

```
pomerado-integrations/example_reader/
├── src/                  # generated operation modules
├── pomerado.json         # entrypoint, input/output schemas, allowed questions
├── sign-in-states.json   # sign-in screens, no secret values
├── deployment.json       # name, URL, task, authority, sign-in origins
├── mcp.mjs / mcp.json    # launcher + standard MCP entry (no key)
└── README.md
```

Optional **HTTP operations** (`defineHttpOperation`, `SiteHttp`, `readText` / `readJson`, HTML/embedded-JSON parsers) can skip the browser entirely when a host supplies an `HttpTransport`. The stock local host does not mint HTTP versions; a custom host can.

### Models and providers

Stock minting expects an **OpenAI API key**. Getting-started docs name **`gpt-6.1-sol`** and **`gpt-6-luna`**. The agent that *calls* a minted tool can be any model; **only mint/review uses those models**. The library allows replacing providers:

> `minterProvider`, `guardianProvider` and `outcomeReviewerProvider` take Agents SDK `ModelProvider` implementations in place of the default OpenAI provider.

A custom Node host can therefore mint with OpenRouter. Stock `pomerado-mcp` does not document an OpenRouter switch.

### Browser attachment (critical)

Documented `pomerado-mcp` flag:

- `--endpoint <ws://...>` attaches with Playwright **`chromium.connect()`**. Playwright versions must match.
- A Chromium **remote-debugging / `connectOverCDP()` URL is explicitly unsupported** on this path.

The **core executor** (`pomerado/core/execution/playwright-execute`) is broader:

> It launches Chromium, attaches to a Playwright browser server (`endpoint`), or attaches over CDP to the own page of a running Chromium a **host owns** (`cdp`). … The **local host doesn’t attach over CDP**, since its runs close other tabs and reset site data.

Implications:

- Out of the box, Pomerado is **not** “drive the user’s already-open Chrome.” Served runs open a **new context** (no cookies) unless you use the library session (`createPomerado`), which can reuse one signed-in context.
- A **custom host** we write can pass `cdp` and, carefully, attach to a Chromium we launched or relayed. Stock MCP will not. Closing other tabs / resetting site data is exactly what we must *not* do on the user’s daily browser.
- `--headed` is required for human browser hand-off (a declared `browser_handoff` step, up to 30 minutes).

### Python backend integration

Pomerado is Node. The Python orchestrator should treat it as a **sidecar**, not a rewrite.

**Path A — stock MCP (simplest, good for v1 logic):**

1. Require Node 24.21+ and `npx -y -p pomerado playwright install chromium` on the machine.
2. From FastAPI, spawn `pomerado-mcp mint --root /abs/integrations` as a **stdio MCP server**.
3. Use the official Python MCP client to call `mint`, poll `get_job` (waits up to 30s), `provide_input`, `cancel_job`.
4. On `"build": "published"`, spawn the integration’s `mcp.mjs` (or `pomerado-mcp serve --artifact <dir>`) and call the tool. **Zero tokens.**
5. Forward interactive questions (login, 2FA, confirms) to the Chrome extension / UI over the backend WebSocket.

**Path B — thin Node host (needed for OpenRouter minting and CDP):**

1. A small Node process composes `pomerado/core/*` (`createPomerado`, or the hook interfaces in “Other hosts”).
2. Inject OpenRouter-backed `ModelProvider`s for minter / Guardian / outcome reviewer.
3. Optionally pass a host-owned `cdp` endpoint (for example a Playwright-launched Chromium, or later a constrained CDP relay).
4. Python talks to that process over stdio MCP, or a local HTTP/WS control plane.
5. Pin the npm package exactly and read `package.json#pomerado.hostProtocol`.

**Path C — Pomerado Cloud REST** (`https://api.pomerado.ai/v1`): hosted mint, run, OAuth brokerage, breakage detection and auto-repair. Useful later; not required for a local-first backend.

Minted scripts sit in the **deterministic layer**: they are reviewed, schema-checked TypeScript operations, not prompt traces. On breakage, the platform should either run Pomerado maintenance/repair (owner-confirmed contract changes; Cloud advertises automatic repair) or fall back to browser-use and then re-mint / patch `src/`.

### Fit for this platform

Pomerado is the best match for “AI only while building; deterministic and fast at run time.” It is also the most opinionated (OpenAI-by-default mint, Playwright not stock CDP, new browser context on served runs, Node 24). Plan the sidecar early.

---

## 5. Other “pomerado” lookalikes (ruled out)

| Candidate | Why it is not it |
| --- | --- |
| **Pomodoro** | Timer technique / apps. No browser-automation minting. |
| **Pomerium** | Identity-aware proxy. Adjacent to access, not subagent workflows. |
| **Playwright / Stagehand / browser-use** | Real tools, but not the name Alex used. |

No other similarly named browser-agent repo turned up. **`Pomerado/pomerado` is the identification.**

---

## 6. Other relevant frameworks

For each: contribution to deterministic-first + AI-fallback, and whether it can drive a user’s real Chrome via extension or CDP.

### Stagehand (`browserbase/stagehand`) — MIT, TypeScript + Python SDK

- Primitives: `act()`, `observe()`, `extract()`. `observe()` returns an `Action` (selector, method, args); passing that Action to `act()` **replays with no inference**. `selfHeal: true` re-infers once if the cached selector is gone.
- Server-side cache of `act` / `observe` / `extract` exists, but **only on Browserbase-hosted browsers** after project enablement. Local browsers do not get that cache (`cache` is a no-op).
- Local attach: `localBrowser.connect({ cdpUrl: "http://127.0.0.1:9222" })`. v4 can load a bundled Chrome extension over CDP when the browser exposes DevTools and shares a filesystem.
- **Use:** pattern to copy (observe → deterministic act → self-heal). Optional Python dependency. Do not rely on Browserbase cache for a local-extension product.

### Skyvern (`Skyvern-AI/skyvern`) — **AGPL-3.0**, Python (+ TS client)

- Vision/LLM agent plus a no-code workflow builder. After a successful run it can **generate Playwright code**, then `run_with="code"`. AI fallback / self-heal regenerates code when the page drifts. Claimed 3–5× faster, up to 70% cheaper on cache hits.
- **Can drive the user’s Chrome** via CDP: `chrome://inspect/#remote-debugging`, then `browser_address="http://127.0.0.1:9222"` or `connect_to_browser_over_cdp`. Also cloud + tunnel.
- Closest **production-looking** “cache the script, fall back to the agent, rewrite the script” loop. Same AGPL problem as workflow-use. Study the loop; do not embed without a license decision.

### Playwright MCP (`microsoft/playwright-mcp`) — Apache-2.0

- LLM talks to the page through the **accessibility tree**, not screenshots: fast, token-lighter, deterministic tool application.
- `--codegen` emits TS/Python/Java/C#. `--cdp-endpoint` (including `chrome`) attaches to running Chrome. **`--extension`** connects through the official Playwright Chrome/Edge extension to existing tabs, cookies, and SSO — the cleanest vendor path onto a user’s real session.
- Better as a **recording / attach substrate** and a11y snapshot source than as the whole subagent runtime (MCP tool schemas are chatty if used naively on every step). Microsoft themselves now push CLI + skills for token efficiency.

### nanobrowser (`nanobrowser/nanobrowser`) — Apache-2.0, ~14.4k stars

- Chrome/Edge **extension agent** with a side panel. Keys and model settings live in the extension — the UX Alex described.
- Hard split: `src/browser-control/` is **deterministic and LLM-free**, talking to the page only through **`chrome.debugger` (CDP)**; Puppeteer is used only for frame / OOPIF discovery, not for clicking. Element IDs are bound to a specific node (stale IDs fail closed). Actions are real user input with visibility / occlusion checks and structured receipts.
- Not a workflow compiler. Best **reference architecture for our extension + control layer**.

### Steel (`steel-dev/steel-browser`) — Apache-2.0, 8,003 stars, last push 23 November 2026

- Self-hostable or cloud **browser infrastructure**: sessions, CDP WebSocket, proxies, stealth, extensions, live view. Playwright `connectOverCDP(session.websocketUrl)`. Experimental Commands API (snapshot + refs) for agents.
- **Use:** optional execution backend when we are not in the user’s Chrome (parallel runs, stealth, CAPTCHA). Does not itself mint workflows.

### Browserbase

- Hosted browsers used by Stagehand. Caching, stealth, session features. Cloud dependency; not required for a local-first backend. Same niche as Steel / Browser Use Cloud.

### rrweb (`rrweb-io/rrweb`)

- DOM session recording / replay. workflow-use already uses it in the recorder. Useful for **capturing a demonstration** and debugging, not for executing actions or healing selectors. Do not treat replay of rrweb events as a production automation runtime.

### Playwright codegen (core Playwright)

- Records a session to a script. Deterministic, no AI, brittle selectors unless later enriched (roles, getByRole, test ids). Good as a **third capture source** alongside the extension and Pomerado mint.

### Agent-E (`EmergenceAI/Agent-E`) — MIT, Python, ~1.3k stars

- Playwright web agent with published design principles. Last push **4 May 2026** — slower than the 2026 leaders. Educational, not a foundation to bet on.

### LaVague (`lavague-ai/LaVague`)

- Selenium-first agent that *generates and runs* driver code; Playwright driver is second-class; there is a Chrome extension that executes JS via a DriverServer. Feature matrix is uneven. Lower priority than Pomerado + browser-use + a custom extension.

### Self-healing selector libraries (Healenium, etc.)

- Traditional test-tool healing (often DOM-similarity on Selenium locators). Weaker than semantic text + role chains + a bounded LLM repair. Fine as one more fallback in the selector chain, not as the platform.

---

## 7. Cheap, fast OpenRouter models (verified 24 November 2026)

Prices below are **USD per 1M tokens, input / output**, taken from the model pages on [openrouter.ai](https://openrouter.ai) on 24 November 2026 (IST). OpenRouter routes across providers; the **listed** price is usually the cheapest standard-tier provider shown on the page. Latency is P50 as shown on that page for a named provider. Prices move; re-check before shipping.

All six support **tool calling**. Vision and JSON-schema strictness vary.

| # | Model slug | Listed in / out | Context | Vision | Tools / JSON | Latency notes | Role |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `qwen/qwen3.7-flash` | **$0.03 / $0.13** | 1.0M | Yes (text, image, **video**) | Tools + `tool_choice`. Structured output supported; confirm JSON-schema enforcement per provider. | Alibaba Cloud Int.: **0.70 s** P50, ~77 tok/s, 99.93% uptime. Cache read $0.006. | **Recommended default** for fallback: cheap, vision, long context, marketed for multimodal agents and computer interaction. |
| 2 | `openai/gpt-oss-20b` | **$0.0153 / $0.0765** (Darkbloom) | 131K | **No** | Tools, `tool_choice`, JSON schema `response_format` (8 of 9 providers). | Darkbloom ~0.54 s / 75 tok/s. **Groq** ~0.20–0.30 s and **450+ tok/s** at $0.075 / $0.30. CoreWeave ~0.20 s / 135 tok/s at $0.03 / $0.13. | **Cheapest text-only** pick when the prompt is an a11y tree, not a screenshot. Pin Groq/CoreWeave when latency matters more than list price. |
| 3 | `tencent/hy4-flash` | **$0.035 / $0.1405** (Tencent Cloud, 50% off; list $0.07 / $0.281) | 1.0M | Yes (text + images) | Tools, `tool_choice`, JSON schema. | Tencent P50 **2.88 s** (slow TTFT). DeepInfra 1.42 s, PrimaLabs 1.13 s at the higher price. Very high cache-hit rates on Tencent (~95%). Released 16 November 2026. | Good quality/cost if TTFT is acceptable; not the low-latency default. |
| 4 | `z-ai/glm-5.3-flash` | **$0.0356 / $0.0815** (Relace, listed page price). Many providers cluster around $0.15 / $0.50. | 1.0M | Yes (text, images, video) | Tools + JSON schema. `tool_choice` not emphasized on the FAQ the way others are. | Broad provider set. Friendli ~0.18 s / 124 tok/s; Baseten ~0.32 s / 166 tok/s (higher price). Relace (cheapest) ~1.04 s / 78 tok/s. | Strong long-horizon agent candidate; use as a **quality** fallback. Watch provider variance. |
| 5 | `qwen/qwen3.6-35b-a3b` | **$0.05 / $0.70** (Darkbloom). CoreWeave $0.10 / $0.89. | 262K (YaRN to 1M) | Yes (text, image, video) | Tools + JSON schema (most providers). | Darkbloom 0.68 s / 43 tok/s. CoreWeave **0.46 s** / 62 tok/s. | Viable cheap vision MoE; output is pricier than Qwen3.7 Flash. Prefer 3.7 Flash unless a provider or quality test says otherwise. |
| 6 | `deepseek/deepseek-v4.1-flash` | **$0.0498 / $0.1992** (page “83% off” listed price). Many providers $0.10–0.30 in / $0.48–1.20 out. | 1.0M | Yes (native image understanding) | Tools. Positioned for coding, terminal, and **computer-use agents**. | Together ~0.16–0.22 s and ~215 tok/s at a much higher rate. Ionstream / Reka / Wafer also sub-0.3 s on some tiers. | Excellent **speed-at-a-price** option; pin a fast provider. Good second default if Qwen3.7 underperforms on tool calling. |

**Not recommended as the default:** `google/gemini-3.6-flash` at **$0.75 / $3.75** (1.2–1.3 s, vision + tools + 1M context). Fine as a manual “quality” override, not as the factory setting.

**Practical default in the extension UI:**

- Default: `qwen/qwen3.7-flash`
- “Cheapest (no vision)”: `openai/gpt-oss-20b`
- “Faster / stronger”: `deepseek/deepseek-v4.1-flash` or `z-ai/glm-5.3-flash`
- Optional “best”: `google/gemini-3.6-flash`

Route OpenRouter with `provider` preferences (Nitro vs Floor) so the user can pick speed or price. Prefer **a11y-tree / semantic snapshots** over screenshots so even the no-vision model works and tokens stay low.

**Pomerado minting** is a separate model budget. Stock path wants OpenAI (`gpt-6.1-sol`, `gpt-6-luna`). Unless we build the custom ModelProvider host, the user’s OpenRouter key alone will not mint Pomerado tools.

---

## 8. Proposed architecture

Alex asked for the backend and the logic first, in Python, with a later Chrome extension or web app.

### 8.1 Layers (deterministic first)

```
┌─────────────────────────────────────────────────────────────┐
│  Chrome extension / later web app                           │
│  - chrome.storage.local: OpenRouter key + model             │
│  - recorder UI, run UI, 2FA / confirm prompts               │
│  - chrome.debugger control plane (nanobrowser-style)        │
│  - WebSocket to local (or remote) FastAPI                   │
└───────────────────────────┬─────────────────────────────────┘
                            │ WS (actions, traces, questions)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Python FastAPI orchestrator                                │
│  Subagent registry, jobs, workflow store, policy            │
└──┬───────────────┬──────────────────┬───────────────────────┘
   │               │                  │
   ▼               ▼                  ▼
 Recorder     Deterministic       LLM repair
 (capture)    runtime             (last resort)
```

**Layer 0 — Acquire an artifact (AI allowed once)**

- Natural language → **Pomerado mint** (sidecar) → TypeScript operation + schemas.
- Or: user demonstrates in the extension → semantic workflow JSON (workflow-use ideas, our code).
- Or: one **browser-use** run → compile history into the same workflow schema (HealingService idea).
- Or, later: Playwright codegen / Playwright MCP `--extension` as another capture source.

**Layer 1 — Deterministic replay (zero tokens)**

- Prefer a Pomerado artifact when one exists: `serve --artifact` / MCP call, schema-validated in/out.
- Else run our workflow JSON: no model.
- **Selector fallback chain** (try in order, no LLM): test-id / `data-*` → accessible name + role (`getByRole`) → label / placeholder / visible text → CSS → XPath → last known coordinates only if explicitly allowed.
- Waits and verification: navigation, URL, enabled/visible, input value, network quiet. Fail closed.

**Layer 2 — Heuristic / cached-action repair (still no LLM if possible)**

- Retry with the next selector in the chain; widen waits; semantic nearest-text / ARIA match (Stagehand `observe` cache locally — we store it, not Browserbase).
- Optional: Pomerado HTTP path or cached route when the page is just a JSON/HTML fetch.
- Circuit breaker: N failures in a step or globally → escalate.

**Layer 3 — LLM repair (bounded, then write-back)**

- Short episode only: failed step + workflow overview + a11y snapshot (screenshot only if the chosen model has vision).
- Executor: **browser-use 1.0** on the same browser (`cdp_url` or `Browser('chrome')`), OpenRouter model from the extension.
- Hard caps: e.g. 5–8 steps, tight timeout, domain allowlist, secret redaction (browser-use already has `sensitive_data`).
- **On success, compile the repair back into the artifact** (patch selectors / Pomerado `src/` / re-mint). This is the step workflow-use has not finished and is the difference between “an agent that sometimes helps” and a platform that gets cheaper over time.

**Layer 4 — Optional human hand-off**

- Pomerado already has `browser_handoff`. The extension can offer the same for logins, CAPTCHAs, and irreversible submits.

### 8.2 How the Chrome extension talks to the backend

Recommended v1 (local-first):

1. Extension service worker opens a **WebSocket** to `ws://127.0.0.1:<port>` (or a user-configured remote URL with TLS).
2. For **driving the user’s real Chrome**, the extension attaches via **`chrome.debugger`** and either:
   - executes deterministic commands locally (nanobrowser control layer), reporting receipts to the backend, or
   - **relays CDP** to the backend so browser-use can use `Browser(cdp_url=...)`.
3. Enabling `chrome://inspect/#remote-debugging` remains a simpler fallback for browser-use / Stagehand / Skyvern without a relay.
4. Pomerado stock MCP still wants a **Playwright browser server**. Until a custom host uses core `cdp` attach, mint/run Pomerado tools in a dedicated Chromium (library session can reuse a signed-in context; user signs in through `provide_input`). Do not let stock Pomerado “reset site data / close other tabs” on the daily browser.

Playwright MCP `--extension` is a useful interoperability option (SSO, existing tabs) but should not be the only control path.

### 8.3 Where the API key lives

- User pastes the OpenRouter key in the extension options page and picks a model.
- Store in **`chrome.storage.local`** (or `session` if we want tighter lifetime). Optional OS-backed wrapping later; not required for v1.
- The key is sent to the **local** backend only when a run needs an LLM (or Pomerado custom host), over localhost WS, as a request header / first-message credential. **Do not write it to server logs or a database by default.**
- The backend may hold it in memory for the job. Cloud deployment later needs an explicit “I trust this server” setting.
- Pomerado mint, if using stock MCP, needs a **separate OpenAI key** in the sidecar environment — surface this in the UI so it is not a surprise.

### 8.4 Language choice

**Python FastAPI for the orchestrator** (as Alex chose), because browser-use and the workflow-use reference design are Python, and the first milestone is logic, jobs, and the repair loop.

**Node 24 sidecar for Pomerado** (subprocess + MCP client, later a custom `pomerado/core` host). Do not reimplement the minter in Python.

TypeScript can come later for the extension and for any Pomerado host. A pure-TS backend would fight browser-use 1.0 without a good reason.

### 8.5 License posture

| Use freely (permissive) | Needs an explicit AGPL decision |
| --- | --- |
| browser-use (MIT), Pomerado (MIT, current), Stagehand (MIT), nanobrowser (Apache-2.0), Steel (Apache-2.0), Playwright / Playwright MCP (Apache-2.0) | workflow-use (AGPL-3.0), Skyvern (AGPL-3.0), Pomerado ≤0.1.2 (historical AGPL) |

Recommendation: implement our workflow schema and recorder ourselves, inspired by the AGPL projects; depend at runtime on MIT/Apache libraries; optional AGPL adapters behind a plugin boundary if Alex wants them later.

---

## 9. Open questions for Alex

1. **First concrete task** to compile and replay end-to-end (the earlier question still stands). That pins selector strategy and auth needs.
2. **Must subagents drive the already-open, logged-in Chrome**, or is a dedicated Chromium that the user signs into once (Pomerado library context / saved storage state) acceptable for v1?
3. **OpenAI key for Pomerado minting:** acceptable in addition to OpenRouter, or must minting also go through OpenRouter (custom host work)?
4. **AGPL:** are workflow-use / Skyvern allowed as dependencies, or reference-only?
5. **Where the repo lives** (new GitHub repo vs only on this machine).
6. **Cloud browsers** (Browser Use Cloud, Steel, Browserbase, Pomerado Cloud) in v1, or local-only?
7. **Write-action policy:** always confirm in the extension before submit / pay / delete (nanobrowser-style)?
8. **Multi-user / remote backend:** is v1 single-user local, or should the API already assume a hosted orchestrator?

---

## 10. Sources (primary)

- browser-use README, AGENTS.md, MIGRATING.md, 1.0.0 release notes (10 Nov 2026), docs: [docs.browser-use.com/open-source/browsers](https://docs.browser-use.com/open-source/browsers)
- workflow-use README, LICENSE (AGPL-3.0), issue #146, DeepWiki architecture pages, release v0.2.11
- Pomerado README, `docs/how-it-works.md` (full file, including runtime / library / other hosts), `docs/getting-started.md`, [docs.pomerado.ai](https://docs.pomerado.ai), [pomerado.ai](https://pomerado.ai)
- Stagehand v4 docs (act, caching, browser connect)
- Skyvern README and browser / code-cache docs
- Playwright MCP README; Playwright `connect` vs `connectOverCDP`; Playwright MCP extension mode
- nanobrowser README / developer docs
- Steel docs (CDP, Playwright); GitHub API for stars/license (23 Nov 2026 push)
- OpenRouter model pages, fetched 24 Nov 2026: [qwen3.7-flash](https://openrouter.ai/qwen/qwen3.7-flash), [gpt-oss-20b](https://openrouter.ai/openai/gpt-oss-20b), [hy4-flash](https://openrouter.ai/tencent/hy4-flash), [glm-5.3-flash](https://openrouter.ai/z-ai/glm-5.3-flash), [qwen3.6-35b-a3b](https://openrouter.ai/qwen/qwen3.6-35b-a3b), [deepseek-v4.1-flash](https://openrouter.ai/deepseek/deepseek-v4.1-flash), [gemini-3.6-flash](https://openrouter.ai/google/gemini-3.6-flash)

