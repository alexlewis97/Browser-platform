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
