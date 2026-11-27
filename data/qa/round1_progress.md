# QA round 1 progress (Asia/Jerusalem)
- 528b9fd local commit (not pushed): core fixes.
- Uncommitted train-iteration fixes: unstated-value check (normaliser span verified by code), date grain from name/ISO examples,
  grounding precision (numbers + quoted text only; ids/urls ignored; code-resolved relative dates allowed; one rewrite retry),
  declined plans not executed, public_job.declined.
- Staging: container browser-platform, /tmp/bp-staging (src, data copy migrated, qa harness), uvicorn 127.0.0.1:8799, pid in /tmp/bp-staging/server.pid,
  Chrome CDP 9231 profile /tmp/bp-staging/chrome-profile. Scripts on VM: /tmp/stg_restart.sh, /tmp/stg_eval.sh <train_cases|test_cases> <name>.
- e2e on staging: workspaces 21/21; multi-intent 6/6 (24,031 tokens in vs 21,925 baseline); chat_tools 5/6 (explore_month n/a: no football_fixtures wf in library).
- TRAIN (train4/train_final): 14/19 harness passes; fails = 2 sports-workspace coverage gaps (honest decline/needs_input),
  double-click QA tool has no dblclick action (named failure), checkbox QA tool 'Expand all' selector gone (x2).
- 11:06 resumed: check train_final2, then TEST once, then deploy/migrate/web/commit. No push (no GITHUB_TOKEN).
- 11:08 train_final2 (latest code): 14/19, 0 grounding rejections. Running TEST once (test_final).
- 11:10 TEST (once, test_final): 17/19 harness passes. Fails = bbc_l1_ch_fixtures + bbc_l2_empty_fixtures in Sports: honest "not covered" declines (no competition tool in Sports). Slider chat grounded (extract 75). 0 grounding rejections.
- 11:12 steering: deploy round 1, then features: (1) competition/date params on BBC wfs via explore mode only, (2) relative dates, (3) dblclick/right-click step types (recorder, executor, from-task, extension), (4) list/array inputs. Skip slider/side-panel. Commit locally only.
- 11:13 DEPLOYED round 1 (c85fefe src) to VM: backups /home/ubuntu/src-backup-qa1-202611270912.tgz + data-backup-qa1-202611270912.tgz; 19 src files; /health ok.
  Live migration applied to 7 Sports wfs (recheck 0 changes). Live smoke chat 'Liverpool next match?' ok. Admin web deployed (browser-platform-web.vercel.app index-DZkoSW1Z.js). Staging removed.
- Next: features (explore-mode params, dblclick/right-click, list inputs). Scope rule: authoring + orchestration only; ideas -> data/qa/ideas_backlog.md.

## 11:13 deployed round 1 (c85fefe live, web deployed). Features started.
- F1 explore link families: authoring/explore_links.py (typed path/query/child families, sibling views), derive endpoint POST /api/workflows/{id}/derive (apply=false default), explore sample variables from examples.

## 11:20-12:25 features (local commits 2632ed7..4365c2f)
- F1 explore link families + sibling views + derive (2 passes) + enum/default_view; retrieval prior fixed (new tools sank to last).
- F3 double_click/right_click across executor, recorders, from-task/recording, extension (1.5.0), web remote recorder.
- F4 list inputs (type list, join filter, chat fan-out, array schema, /run rejects lists for one-value inputs).
- Harness: synthesis gets today + code-resolved dates, shared facts budget, weekend expressions.
- Staging evals: reports in /tmp/bp-staging/reports on the VM (deleted at cleanup); copies in /workspace/feat1.

## 12:05-12:15 features deployed live
- Backups: /home/ubuntu/src-backup-feat-202611271005.tgz, data-backup-feat-202611271005.tgz (10:05 UTC). 0 live jobs. 24 src files (c85fefe..7183e0c) copied, restart, /health ok.
- Live explore apply on Sports: team fixtures gained `filter` (results|fixtures). Derived "BBC Football league Scores & Fixtures" (262e3143…, competition + month + filter) from the league table's sibling view and added it to Sports (now 8 tools).
- Live chats: "Championship fixtures on 15 June 2026?" now correct (no events). "Today's Championship fixtures?" now routes but is WRONG: planner sends filter=fixtures, which leaves out today's match.
- Admin web redeployed (live bundle index-BuGuev7a.js). Staging and /workspace/browser-qa in the container removed. Extension 1.5.0 zip in releases/ (web-run not redeployed).

## 12:12-12:26 "today" view bug (generic fix attempt)
- Approach: date semantics drive the view choice. If the question resolves to today and a tool input has an explore-detected `default_view`, drop that input so the page opens in its default view (b249a68). Also: optional unstated inputs are dropped, not declined (5865856); search_text carries input examples (f1c184f). The twin-call approach (2995e2c) was superseded.
- Live (b249a68): today questions now fetch the page's default "Today" view, and the facts contain the missing match (e.g. Southampton v Middlesbrough 20:00).
- Results: TEST Premier League today was correct, and an earlier Bundesliga today run was correct. TRAIN Championship/La Liga today were still wrong: synthesis says "none" because the extract has no printed date and is wrapped in month/Results navigation text.
- Tried 081aa81: a code-stated view note prefixed to the facts. It was worse live (TRAIN 0/2, TEST4 Serie A/Bundesliga 0/2), so I reverted it in 61ff636 and redeployed (12:26, /health ok). The REGRESS "fixtures this month" case was correct.
- Open: synthesis misreads nav-wrapped extracts (a better next step is to strip navigation/boilerplate from facts or extract the list region); the planner omits filter=results for "yesterday's results" (it sent month="yesterday"); the Ligue 1 slug normaliser gap.

## 12:27-12:45 BRO-18 answer side (generic)
- f2711c4: the extract's main/body fallback drops page chrome before the model sees it: semantic nav/header/footer/aside, in-page jump links, short control buttons and link strips (many short links, little other text). Elements are hidden only while innerText is read. Falls back to the raw text if the result is empty or tiny. Chat items carry `page_view` metadata (input, value, or "page default view" plus the page's label). The prose rule was replaced by a schema note. Tense inference: a question wholly in the past/future fills an omitted view enum with the one value whose words say that tense. `match_known_value`: free text maps to an enum value or a slug-format example value when there is a unique match (number words, word subset).
- 3b61041: view controls (inputs with default_view) are not required to appear in the extract; page_view is kept on stored items.
- d186604: only the innermost link strip is dropped (DemoQA books kept its table; Wikipedia lost 4%; BBC nav removed).
- 6f76586: dates_in_question carry weekday names.
- Live 12:44 (backup /home/ubuntu/src-backup-answer-1232.tgz; restarts waited for zero live jobs; /health ok each time).
- Results over 4 post-fix runs:
  - TRAIN (Championship today, La Liga today): 7/9; the misses were synthesis variance in runs 1 and 3.
  - TEST (Premier League, Serie A, Bundesliga today): 9/9.
  - REGRESS (Championship fixtures this month): 3/3.
  - Ligue 1 slug: 3/3 (french-ligue-one).
  - Yesterday's results: filter=results inferred every time. After the fixes it is correct ("no matches Thursday 26 Nov").
- Open: occasional "none" on an undated Today view (synthesis variance, about 1 in 4 TRAIN); a 75k-char Wikipedia-style page is still cut at 4000 chars (unchanged).

## 12:47-13:00 ordering/typo bugs (generic)
- 1728506 + fd4fbd1 (planner/answer code only):
  - facts_order.py parses facts into events (date, time, context). An undated item in a page view labelled Today is dated today.
  - Code computes `events_on_question_dates` (a weekend covers Sat+Sun) and `computed_order` (asc for first/earliest/next, desc for last/latest; date phrases excluded; ties share a rank; undated items listed but not ranked). It is passed to synthesis as `computed`.
  - Post-answer check: if the lead names a lower rank first, or says "none" while items exist, one rewrite is tried, then the answer is built from the computed data.
  - Fuzzy spelling: edit distance (OSA) ≥0.8 against the input's examples/enum. The best match is used and recorded as `resolved` (resolved_from). A tie becomes "did you mean A or B?". A planner-corrected spelling counts as stated when the user's word is a near match. List items are resolved one by one and never dropped.
  - Intent coverage: an unmapped intent naming a step's argument value counts as served (fixed the spurious "partial").
- 2303e9f web-run redeployed 12:53 (https://browser-platform-run.vercel.app/browser-platform-extension.zip serves 1.5.0, byte-identical). Backend live 12:57 (backup src-backup-order-1252.tgz).
- Live (after fd4fbd1):
  - TRAIN: "Tottenham, arsenal or chelse": Chelsea now runs, and the order is consistent with the data (Spurs 14:05 before Arsenal 16:30; Chelsea's item has no printed date and is reported as unranked). Brighton/Fulahm: Fulham resolved; the Brighton run failed (tool run, flaky).
  - TEST 5/6: Wolves/Everton/Leeds, Bournemouth/Palce (tie), Brentford/Nottm Forest this weekend, Celtic/Rangers, PL today all OK. Chelsea/Arsnal: typo resolved, but the answer is "Arsenal first" because Chelsea's tonight game has no printed date on the team page.
  - Typos 4/4 resolved; 0 dropped.
- Open: team pages print today's game without a date. The team fixtures tool has no explore-detected default_view (an authoring gap, BRO-19 area), so code can't date it. The consistency rewrite never fired live (unit-tested only).

## 13:13-13:28 Round 3 pre bugs (generic; planner/answer + workflow_inputs only)
- ddf93d8:
  - Fuzzy scoring is per whole word: exact > truncation prefix ("man") > abbreviation ("utd") > edit distance. Two different words that share a prefix score 0. Every user word must match a distinct option word. A tie keeps the planner's value (recorded as fuzzy_ties).
  - Verified empty pages ("no events to show") get data_status=empty and succeed.
  - Only an enum is closed: examples and value_map labels are hints, so unknown plausible slugs pass. BRO-19's closed-enum needs_input is kept; their tests were updated to that contract.
  - Enum view controls are not required in extracts.
- ca90bd0 (lost in BRO-19's amend/reset; content re-committed in 1a167ff): the normaliser never swaps one user-named entity for another.
- 1a167ff: a focused "implied value" question runs before declining an unstated required value. The span is verified by code, and the result is recorded as `inferred`.
- Deploys: 13:20 (backup src-backup-r3-1320.tgz), 13:24 and 13:27 (normalize_inputs.py.bak-HHMM). /health ok, waited for zero live jobs.
- Live:
  - TRAIN (QA's cases) 5/5: man city weekend → City; liverpool/man utd → United; arsnal/liverpool pts → PL table; Championship 15 Jun 2026 → succeeded "no events"; Scottish Premiership table.
  - TEST 6/9:
    - OK: man utd weekend, spurs/chelse points, PL 20 Jun 2026 empty, barcelona table position (La Liga inferred), man city/newcastle points, "show me the league table" (correctly asks).
    - Fail: Eredivisie (planner slug 'eredivisie' → not-found page, honest failure); celtic/hearts still declined (implied question variance); villa/brighton (right table, but the answer says Villa above while the facts show Brighton 2nd / Villa 12th).
- Open: table-position comparisons are not covered by computed_order (dates/times only); the implied-value question is not 100% reliable.

## Round 3b — numeric table comparisons, implied values from prior runs (463846e, live 13:33 Asia/Jerusalem)
- facts_order: generic table parser (modal-width rows, numeric columns detected per column, rank from leading integer); `table_comparison` computes above/below, more/less, top/bottom in code; `table_conflicts` drives the rewrite-then-fallback check.
- prior_values: deterministic implied value from the workflow's past run outputs (unique option page containing all distinguishing user words); LLM implied-value question is now only the fallback.
- Live: TRAIN 2/2 (Villa/Brighton "No", Celtic/Hearts). TEST 5/5 correct answers (Newcastle below Villa, fewer points Everton/Fulham, best GD, Rangers/Hibs, where are Hearts). REGRESS 3/3.
- Gap: "is rangers above hibs?" fanned out a second call; the normaliser remapped the planner's `scottish-championship` to the non-existent `scottish-premier-league` (failed, ignored in the answer, status partial).
- Gap: Celtic/Hearts live — planner supplied the competition itself, so the prior-runs path was not exercised live (unit-tested and probed against the VM's real runs: Celtic/Hearts → scottish-premiership).
