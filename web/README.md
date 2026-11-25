# Browser Platform — mobile web app

Phone remote control for the FastAPI backend in `/workspace/browser-platform`.

- OpenRouter key, shared access token, model and backend URL are stored in **browser localStorage only**.
- The key is sent per request and never persisted by the backend.
- The browser that performs the work runs on the box (or later, on a laptop Chrome). This site is the remote control.
- The backend URL is configurable because a Cloudflare quick tunnel gets a new hostname if that process restarts.

## Develop

```bash
cd /workspace/browser-platform/web
npm install
npm run dev
```

Build output goes to `site/` (not `dist`, so it is not wiped by box image rotations of build-output folders).

## Deploy

Deployed to Vercel as a static Vite app from this directory.

## Human-in-the-loop

The live run view can show a `needs_confirmation` prompt as tappable candidate cards (scores, optional screenshot, Confirm / Cancel). The API client for this lives only in `src/hitl.ts`. Confirm/cancel routes are not in the backend README yet, so that file is the single place to update when they are documented.


## Human-in-the-loop

The live run view follows the backend README contract:

1. Starts runs with `wait: false`.
2. Polls `GET /api/runs/{id}` (and optionally `WS /ws/runs?run_id=`).
3. When `status` is `needs_confirmation`, renders `confirmation.top` as tappable cards (scores, optional screenshots, Confirm / Cancel).
4. Resumes with `POST /api/runs/{id}/confirmation` `{ "action": "confirm", "candidate_id" }` or `{ "action": "cancel" }`.

The API client lives in `src/hitl.ts`. The run form exposes `hitl_mode`: `always_on_uncertain`, `llm_then_human`, or `llm_only`.

## Create from task

The **From task** screen starts `POST /api/workflows/from-task` with `wait: false`, polls the job, and can confirm detected variables (`kind: "variables"`) via `POST /api/workflows/from-task/{id}/confirmation`. When the job finishes, it shows the saved workflow and creation vs replay tokens and latency, with a Run button.
