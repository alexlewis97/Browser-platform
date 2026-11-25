# Browser-platform improvements

Prioritized backlog from a full-repo review. **P0 and most of P1 are implemented** on this branch; items marked *deferred* remain.

## Done (P0)

- Auth: token required by default when configured; proxy headers (`X-Forwarded-*`) never skip auth; public bind without token refused at startup
- CORS: exact configured origins + localhost only (no `*.vercel.app` / any extension id wildcard)
- WebSocket Origin checks; unused message types rejected; CDP relay not advertised as live
- Chrome CDP: `--remote-debugging-address=127.0.0.1`, no `--remote-allow-origins=*`
- Docs/OpenAPI disabled when an access token is set
- HTTP access token header-only (query still allowed for browser WebSockets only); query tokens redacted in logs
- Extension web-bridge: origin + kind allowlist; no token/settings leak to page
- Recorder: active tab only; password/secret fields scrubbed; non-destructive ping
- Navigation: http(s) only; private/loopback blocked for remote-record; TLS verify restored
- Workflow id path jail + DELETE API; Pomerado artifact directory jail; mint `list_workspace` root jail
- Redaction: `sk-proj-` / hyphenated keys, query tokens, single install of logging filter
- Action confirm: any click/input/key_press counts; Delete/remove/transfer in submit lexicon; MCP enforces required inputs
- Discovery: client + server allowlist (no committed sslip URL); empty `backend.json`
- Windows update: requires HTTPS + sibling `.sha256`; refuses empty update URL
- Docker: non-root user; compose paths fixed (`context: ..`, `../data`); token required notes

## Done (P1)

- Output validation: never re-runs action workflows
- Fuzzy apply failure falls through to LLM; no write-back of unreplayed browser-use selectors
- Pomerado success only for terminal statuses
- Result cache keys include variables + query
- Chat: per-request `llm.api_key`/`model` from phone; extension target only `awaiting_results` when calls exist; synthesize status reflects item outcomes
- JSON Schema optional fields fixed (`required` not always true)
- Isolated Chromium: portable temp dir + process-group cleanup
- `save_openai_key` does not clobber unrelated shell env; `0o600` create
- Background run tasks tracked; `wait=false` persists `running` immediately; from-task job cap; paginated `list_runs`
- Workflow DELETE; GitHub Actions CI; marketplace zip size/magic checks

## Deferred

- Full CDP relay / Playwright MCP bridge (architectural)
- Pomerado mint cancel aborting in-flight spend
- Usage-proxy short-read / cap-reservation hardening (partial)
- Marketplace conditional blob writes / zip-bomb entry caps
- MV3 `chrome.storage.session` persistence for long HITL
- Named Cloudflare tunnel / watchdog env cleanup
- Multi-user marketplace identity
