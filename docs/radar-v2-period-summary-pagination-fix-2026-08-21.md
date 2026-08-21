# Radar V2 period summary and pagination fix

Date: 2026-08-21
Status: production web release accepted

## START_CHANGE_SUMMARY

- User-visible defect: the 7-day and 30-day summary cards were derived from the first bounded
  material page instead of the published period aggregates.
- Secondary defect: the frontend requested `limit=100` and ignored `nextCursor`, truncating period
  material lists above 100 rows.
- Affected files: `v2/apps/web/app.mjs`, `v2/apps/web/index.html`,
  `v2/tools/frontend_console_smoke.mjs`, and `v2/tests/test_stage8_readonly_api_frontend.py`.
- Source of truth: `contracts/v1/public-api.openapi.yaml` and the accepted Stage 8 read-only API
  boundary in `docs/radar-stage8-readonly-api-frontend-2026-08-19.md`.
- GRACE module delta: skip — Radar has no `M-*`/`V-M-*` module map. The explicit affected-file and
  contract scope above is the governed substitute used by prior Radar stages.

## Fix and invariants

- For `7d` and `30d`, summary cards now read `/api/stats?period=<period>` independently of the
  currently rendered material page.
- Period materials and search results follow the opaque API `nextCursor` until it becomes null.
- The script URL has a new immutable cache version, so previously cached browsers receive the fix.
- Issue and yesterday modes retain their issue-specific statistics and material behavior.
- The API contract, database schema, content publisher, Legacy runtime, cron and public data are
  unchanged.

The frontend console regression explicitly selects `7d`, requires the stats endpoint, simulates a
second material page, and verifies the displayed `viewed`, `included` and `cut` totals. Static
contract assertions prevent removal of the stats request or cursor loop.

## Live read-only comparison before application deployment

| Period | Runtime | Viewed | Included | Cut | Near / Mid / Far |
| --- | --- | ---: | ---: | ---: | ---: |
| 7 days | Legacy | 491 | 51 | 440 | 14 / 19 / 18 |
| 7 days | V2 API | 491 | 48 | 443 | 14 / 16 / 18 |
| 30 days | Legacy | 1,213 | 189 | 1,024 | 37 / 57 / 95 |
| 30 days | V2 API | 1,213 | 186 | 1,027 | 37 / 54 / 95 |

Both periods therefore cover the same viewed population. V2's three-material difference is an
editorial/rules outcome, while the larger browser discrepancy was the frontend defect fixed here.

## Verification

- focused Stage 8 regression: 22 passed;
- Ruff format and lint: passed;
- strict mypy: passed;
- full pytest: 158 passed;
- contract validation: 6 schemas, 8 examples, 23 tables, 11 API paths passed;
- JavaScript syntax and frontend console regression: passed;
- secret/Legacy isolation: 110 files and 3 fixtures passed;
- deterministic public production artifact: 26 files, SHA-256
  `cf3fcfcc4da2de821121d1d0760f56cf10f19319468b4794795467baffc67995`.

## Production acceptance

- Source commits: `10a7526a80e45fd0e86a5c5d0dc5c7057d937870` (behavior and regression) and
  `8d6e6264b4a8fbd6deaf01ed04983664794b1172` (immutable cache version).
- Final application package: `app_release_20260821_8d6e626`, SHA-256
  `3bc07ead09f180a41adaed56a2ac20c264ba0125c966cb0a6e22b220ecabc0aa`.
- Only the web pointer changed, to
  `/srv/radar-v2.aipractice.space/releases/app_release_20260821_8d6e626`; API code, content DB,
  Caddy configuration and services were not restarted.
- Public HTML references `app.mjs?v=20260821-period-stats-pagination`; the public script SHA-256
  is `0b9a055b47ad782975b7962987de42a4e6257a5055110700199afeff0e95bbae`, identical to source.
- Public 7d/30d stats and health passed; `radar-v2-api.service` and `caddy.service` are active with
  `NRestarts=0` and no error journal entries during deployment.
- Rollback targets and incoming hashes are retained under
  `/root/radar-v2-web-deploy-backup-20260821T0609Z` on Local Ru. No prior release or failed
  create-only target was deleted.
