# Radar frontend period-switch race fix

Date: 2026-08-22
Status: fixed and accepted in Legacy and V2 production

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

Production acceptance:

- implementation commit: `097d3e93fc9cf177956a88994d5be8b85666d343`;
- Legacy pre-change full static-root backup:
  `/root/radar-legacy-period-race-backup-20260822T142700Z/static-root-before`, aggregate file-list
  SHA-256 `eddf5cd598a696ed6a8698c6a446811b3cf4b5f91a312ec94cd2f0c03b6fdf32`;
- Legacy production now serves `app.js?v=20260822-period-switch-race`; public/source script
  SHA-256 is `e41d619f64e226cc81a008926eec61211c456041765ffefb024fc39c2e637377`;
- canonical V2 application package SHA-256:
  `86e824e6a02e57d3da1f57c947c2bdb16cd5f95550950a87da08af7111a2619c`;
- canonical V2 web role artifact SHA-256:
  `19a2078096b0a96a93efbacc34964d647a35b1cb6ffd1057672c3c48c6e529c1`;
- Local Ru V2 pre-change full web-release backup:
  `/root/radar-v2-period-race-backup-20260822T142700Z/web-before`, aggregate file-list SHA-256
  `426375650510f7331d4cd8b22cf94449df2fb6b6b574b7144754687e02387615`;
- previous V2 web pointer retained at
  `/srv/radar-v2.aipractice.space/releases/app_release_20260821_530a3c5`;
- new immutable V2 web pointer:
  `/srv/radar-v2.aipractice.space/releases/app_release_20260822_097d3e9`;
- V2 public/source script SHA-256:
  `b4e9b46f04364f18d8ded250caa34b2e2745cd4a8ed89e652839da6df2517d4f`;
- final three-cycle delayed-response Playwright stress: V2 8 summary / 8 blips / 8 cards;
  Legacy 10 / 10 / 10; no console/page errors, loading residue or horizontal overflow;
- V2 API/Caddy remained on PIDs `70308` / `1025`, Legacy API/Caddy on `1885019` / `259034`;
  all four services are active with `NRestarts=0`;
- V2 was intentionally a web-only deployment. API health therefore retains application release
  `app_release_20260821_530a3c5`; content release `rel_473de6c600563860305e2bd0`, database state,
  API code and process are unchanged.

Rollback remains immediate and retained: restore the Legacy static backup (or revert the source
commit), and atomically point V2 `current` back to `app_release_20260821_530a3c5`. No rollback was
needed.
