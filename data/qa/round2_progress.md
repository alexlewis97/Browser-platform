# QA round 2 execution fixes (Asia/Jerusalem)

Train/held-out split: `data/qa/round2_split.json`. Unit tests: `tests/test_qa_round2.py`.
261 unit tests passed (18 live/e2e deselected). No push.

## Fixes (generic; not site/league/entity-specific)
- Bug 1: `match_known_value` (reused/extended) maps aliases such as la-liga -> spanish-la-liga in code. Closed slug/enum/value_map misses return `needs_input`. No in-run LLM repair.
- Bug 2 / Alex rule: in-run LLM and browser-use repair is kept but hard-disabled. Only `BROWSER_PLATFORM_ALLOW_IN_RUN_REPAIR` (default false) can turn it on. No per-run/API/chat/extension override.
- Bug 3: under `strict=true`, a broken click fails with `strict_selector` and names the step; no fuzzy rescue. (Platform default is still off — unanswered to Alex.)
- Bug 4: explore reuses an unused required input when its examples/labels match a link family (value_map for Travel -> travel_2). Extract "titles and prices" is no longer treated as a page heading. From-task jobs are persisted under data/from_task_jobs so wait=false poll no longer 404s after a restart.
- Bug 6: API/unattended `/run` sets `allow_hitl=False` unless `interactive: true`. Uncertain clicks fail clearly instead of pausing 300s.
- Bug 7: the runner returns `needs_input` for missing required inputs (does not start the browser). Chat "no skill call was planned" is planner-side and was not changed.
- Bug 8: in-run repair was the heavy path after a long failure (tens of thousands of tokens). Disabling it is the cheap mitigation. Jobs now survive a process restart. Cause of the empty 150ms 502 (event-loop stall or brief process drop) not fully proven from logs in this pass.

Bug 5 (answer contradicts facts) and chat planner/grounding were not touched.
