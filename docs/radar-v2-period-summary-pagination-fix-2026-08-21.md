# Radar V2 period summary and pagination fix

Date: 2026-08-21
Status: locally accepted; production application release pending

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
