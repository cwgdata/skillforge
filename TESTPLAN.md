# SkillForge Functionality Test Plan

App under test: `https://skillforge-<workspace-id>.aws.databricksapps.com`
Execution: browser (authenticated workspace session). API-level checks noted
inline where the browser can't isolate behavior.

**Test identity note:** `skillforge-test@cwgdata.dev` exists with full app
permissions (app CAN_USE, warehouse CAN_USE, UC grants on `skillforge.core` —
incl. MODIFY on `injected_prompts` / `mining_config` / `user_state` — and
`skillforge_inference.feeds`). SSO workspaces don't allow password login for
synthetic identities, so per-user-isolation cases are validated by grant
inspection + the API isolation tests already in CI history; browser execution
runs under the operator's session.

## T1 — Load & identity
- T1.1 App loads; sidebar (6 nav items), header controls render; no console errors.
- T1.2 Identity chip shows signed-in email + green **OBO** badge.
- T1.3 Sidebar scrollspy: clicking each nav item scrolls to its section and
  highlights the active item.

## T2 — Overview KPIs & charts
- T2.1 KPI row shows: prompts analyzed (487), users (30), skills recommended
  (14), consolidated %, est. monthly token savings, avg quality lift (numeric,
  not NaN).
- T2.2 Charts render (prompts/day line, tokens-by-endpoint doughnut); source
  pill shows **LIVE UC** (or SNAPSHOT fallback — record which).
- T2.3 Window selector 7d/14d/30d/All changes the charts' data.

## T3 — Patterns
- T3.1 Discovered Patterns table lists 14 rows with prompt/user counts, tokens,
  purity badges (spread, not all 100%).
- T3.2 `results.view` reflects baseline (no "personal view" chip before any
  mutation).

## T4 — Skills
- T4.1 14 skill cards with priority badges and value chips.
- T4.2 Expanding a card reveals template, parameters, example invocation.
- T4.3 Top-3 cards show precomputed Before/After A/B panels with scores.
- T4.4 Export .md downloads a markdown file (frontmatter + template);
  .json downloads the spec.
- T4.5 "Run quality A/B" on a card without one: spinner → panel appears with
  scores + rationale (FMAPI round trip, 1–3 min). Identity chip now shows
  **personal view** + Reset link.

## T5 — Gateway Coverage
- T5.1 Banner: "N of M endpoints have inference tables — X feeds enabled".
- T5.2 `databricks-claude-haiku-4-5` row: green dot, V2 badge,
  `skillforge_inference.feeds...payload`, Tokens(7d) > 0.
- T5.3 No foreign-workspace tables listed (workspace_id filter).
- T5.4 An endpoint without payload capture shows "Create…" link → opens its
  /ml/ai-gateway page in a new tab.
- T5.5 Mine toggle: flip haiku OFF → toast + banner count drops; flip back ON.

## T6 — Inject & history
- T6.1 Inject one prompt → success note (sent=1, inserted=1).
- T6.2 "Clear prompt history" → warning dialog mentions permanent/all users;
  cancel does nothing; confirm → toast with cleared count.

## T7 — Refresh
- T7.1 Refresh click → progress banner (spinner, phase text, percent bar,
  elapsed) while running; button disabled.
- T7.2 Completion → toast summarizing assigned/new; injected prompt from T6.1
  classified; KPI "prompts analyzed" increments (the old stale-KPI bug).
- T7.3 Second refresh click while running returns a clean "already running"
  error (API check: 409).

## T8 — Test Bench
- T8.1 Pick a skill, fill params, Run → answer renders with token counts.
- T8.2 Optional raw-prompt comparison renders side-by-side.

## T9 — Personal view & reset
- T9.1 After T4.5/T7, "personal view" indicator shows; Reset view →
  confirm dialog → back to baseline (A/B from T4.5 gone, KPIs reset).

## T10 — Pending state (API-only)
- T10.1 GET /api/results returns backlog counts if results.json were absent
  (validated by code review/local test; not exercised against prod).

---

## Execution record — 2026-06-10

**Functional pass (API-level, as cliff.gilmore via OBO): 19/19 PASS.**
Two initial "failures" were stale personal-overlay state from an interrupted
earlier run (baseline showed 19 patterns / 937 prompts); the reset case (T9.1b)
restored exactly 487 / 14, proving the overlay mechanism rather than a defect.
Notables: refresh lifecycle 202 -> 409-while-running -> done with KPI bump;
injected probe classified; export headers correct; mining toggle round-trip;
workspace-filtered V2 coverage.

**Visual pass (browser, Chrome DevTools): V1-V11 PASS.**
Dark theme, sidebar scrollspy, KPI tiles (no NaN), painted charts, purity
spread badges, expandable skill cards with A/B panels, coverage badges +
Create links, clear-history warning dialog (cancelled), refresh progress
banner captured mid-run, test-bench answer with token counts, personal-view
indicator + reset.

**Known minor issues:** favicon 404; 6 form fields lack label/id (a11y).

**State wiped post-run:** user_state / injected_prompts / mining_config
emptied, in-process cache reset, baseline verified (14 patterns, 487 prompts,
personal=false).
