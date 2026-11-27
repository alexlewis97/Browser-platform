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
