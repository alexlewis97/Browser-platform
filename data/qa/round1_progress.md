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
