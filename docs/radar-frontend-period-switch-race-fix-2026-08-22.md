# Radar frontend period-switch race fix

Date: 2026-08-22
Status: local implementation accepted; production acceptance pending

## START_CHANGE_SUMMARY

- User-visible defect: rapidly switching from `30d` back to the current issue could leave the
  daily summary selected while the sonar and material columns displayed the late 30-day response.
- Reproduced before the change on both public contours with a deliberately delayed 30-day response:
  V2 rendered 191 stale blips with an 8-item issue summary; Legacy rendered 196 stale blips with a
  10-item issue summary.
- Affected files: `work/radar-app/app.js`, `work/radar-app/index.html`,
  `v2/apps/web/app.mjs`, `v2/apps/web/index.html`, `v2/scripts/verify.sh`,
  `v2/tests/test_stage8_readonly_api_frontend.py`,
  `tools/frontend_period_switch_race_smoke.mjs`, and this report.
- Source of truth: the Legacy-parity frontend contract in
  `docs/radar-stage12a-legacy-frontend-parity-2026-08-20.md` and the period data contract in
  `docs/radar-v2-period-summary-pagination-fix-2026-08-21.md`.
- UI invariant: one committed render must come from one immutable request snapshot. An older
  response may populate a correctly keyed cache, but it must never overwrite materials, loading
  state, summary, sonar, theses or columns after a newer reload has started.
- Non-goals: no API, database, content, pipeline, Project Manager cron, Caddy, DNS, systemd or KX
  changes.
- GRACE-Delta: skip — Radar has no `M-*`/`V-M-*` module map or canonical `design.md`. The explicit
  affected-file, source-of-truth, invariant and regression scope above is the governed substitute.

## Implementation

- Both frontend copies capture `period`, effective issue date, perimeter and search query before
  starting asynchronous work.
- Each reload receives a monotonically increasing generation. Only the latest generation may
  commit returned materials or clear loading state; late success and late failure are ignored.
- V2 period statistics capture the requested period before `await`, preventing a response from
  being cached under a subsequently selected period.
- Both HTML entrypoints use a new cache-bust identifier.
- A deterministic Node smoke executes both real frontend scripts against controlled out-of-order
  responses and requires the final current-issue sonar, summary and columns to contain only the
  current-issue material.

## Verification and production acceptance

Local verification:

- deterministic out-of-order response smoke: PASS for both real frontend scripts;
- patched-script Playwright smoke against both live DOM/API contours: PASS — V2 finished with
  8 summary items / 8 sonar blips / 8 cards; Legacy finished with 10 / 10 / 10, with no console
  errors, loading residue or horizontal overflow;
- Ruff format and lint: PASS;
- strict mypy: PASS;
- full pytest: 181 passed;
- contract validation: 6 schemas, 8 examples, 23 tables and 11 API paths passed;
- JavaScript syntax, frontend console smoke and isolation scan: PASS;
- deterministic public runtime artifact: 26 files, SHA-256
  `5db30885fe1b41bed689a97206b006d179874696d32c52ba098284c46f682308`;
- `git diff --check`: PASS.

Production backups, activation, rollback targets and post-deploy stress results are pending.
