# Ideas backlog (out of scope for the current rounds)

Scope rule: only tool authoring or workspace orchestration, generic only. These were noticed but not built.

- Generalise the BBC-specific `_BBC_TEAM_FIXTURES` regex in authoring/explore.py; the generic link-family detector (explore_links.py) now covers the same case and the regex could be removed after a regression check.
- QA tool fix: the DemoQA double-click QA workflow records a plain click; re-author it with the new double_click step type (site-specific, so not done here).
- DemoQA checkbox "Expand all" selector no longer exists; the QA workflow needs repair/re-recording (site-specific).
- Sports workspace coverage gaps (competition-level fixtures by date, cricket by competition): can now be made with explore → sibling view → derive, but creating live tools is Alex's call.
- Alias-as-fallback policy: should a matched alias count as a fallback (needs repair) or a normal match?
- Daily date pickers rendered only by JavaScript (no links) are not detected by link families; a rendered-DOM pass could add them.
- Sibling-view naming uses the link text; an LLM name/description pass would read better in the planner.
- "Today's X fixtures" with a fixtures/results control: the planner picks filter=fixtures although the page's default (omitted) view is "Today"; the fixtures view leaves out today's games. Needs a better generic rule for controls whose omitted view is date-scoped.
- Synthesis model sometimes contradicts itself on day-level questions over month data (n1 Serie A tomorrow). A stronger synthesis model or a code-side day filter for dated listings could help.
- Month names in URLs (Portal:Current_events/October_2026) are not detected as month families; needs a "Month_YYYY" render filter.
- The existing explore tab regex proposed a bogus "competition" tab on Wikipedia (pre-existing, not changed).
- From-task authoring does not propose list-type inputs yet; authors can set type "list" by editing the definition.
- The extension Playwright e2e specs need the Playwright chromium build and a token on the box to run.
