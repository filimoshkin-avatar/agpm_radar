# Radar V2 Stage 8 — read-only API and frontend

Date: 2026-08-19
Status: accepted locally; no production activation

## Scope and invariants

Stage 8 implements the frozen eleven-route public API and the dependency-free frontend over a
disposable Radar V2 active-release root. The source of truth is
`contracts/v1/public-api.openapi.yaml`, `contracts/v1/sqlite-contract.yaml` and the accepted Stage
7 `active.json`/immutable-release boundary.

The work did not edit Legacy code/data, restart services, install cron, change Caddy/DNS, connect to
Local Ru or activate a real V2 production database. The only schema evolution is application-owned
migration `0002_public_api_views.sql`, which adds three published-filtered views; it changes no table,
column or `user_version` and cannot enter a content candidate.

## Implemented API boundary

- `ActiveDatabaseManager` reads the private atomic pointer, pins a single-link `0600` SQLite inode,
  opens it through `/proc/self/fd` with `mode=ro&immutable=1`, enables `query_only`, verifies
  application/schema/compatibility/integrity/FK/FTS/release/logical-state markers, and swaps the
  cached connection only after a stable pointer reread.
- The SQLite authorizer permits SELECTs only through the contract public views, allows only the
  small function set needed by those queries, and denies direct internal-table reads, PRAGMA,
  writes, schema actions, recursive SQL and `load_extension`.
- Explicit DTO queries implement `/api/health`, `/api/latest`, `/api/issues`, issue-by-date,
  `/api/materials`, `/api/search`, `/api/stats`, `/api/timeseries`, `/api/rubrics`, `/api/sources`
  and `/api/gazettes`.
- Query targets, UTF-8, duplicates, unknown parameters, integers, enums, text, cursors, response
  bytes and search rate are bounded. Errors use the frozen JSON `Error` DTO and never include SQL,
  filesystem paths, exception text or material content.
- Period/search filtering operates on validated public DTOs. User text never becomes SQL or FTS
  syntax. Every summary is derived from a fully validated published issue.
- `pub_material_rubrics_v1` provides rubric IDs/titles for published issue-material rows.
  `pub_material_quality_v1` lets the projector prove historical date anomalies without exposing
  quality internals in response DTOs.
- `pub_gazette_assets_v1` binds every served gazette file to published manifest SHA-256, byte-count
  and media-type metadata. Unlisted or modified files are never served.
- Native V2 rejects a material date after the issue window. A `legacy_inferred` row is compatible
  only when material/quality statuses agree, the stored day delta is exact, and the anomaly is
  explicitly `medium|high` plus `queued`. This covers three known imported-history anomalies while
  preserving the native invariant.
- The stdlib HTTP transport binds only to loopback, applies a socket timeout, returns security
  headers, closes non-GET requests, and avoids logging raw targets/query content.

## Frontend and static boundary

- Same-origin dependency-free HTML/CSS/ES module with latest, archive, historical issue, search and
  gazette index routes.
- Explicit empty-issue and complete LLM-outage notices; fallback state is not presented as primary
  model success.
- DOM nodes use `textContent`/`setAttribute`; there is no HTML-string injection, remote dependency
  or unsafe external URL scheme/userinfo.
- Responsive desktop/tablet/mobile layouts, keyboard skip link, focus-visible controls,
  `prefers-reduced-motion`, live regions and readable empty/error states.
- SPA routes, exact application assets and gazette trees are separate. Unknown assets, missing
  gazettes, plain/encoded traversal and unknown real-file paths return 404 instead of the SPA HTML.
- HTML is `no-store`; versioned assets are immutable-cacheable. CSP differs intentionally between
  the SPA and script-free gazette content.
- Gazette files are read beneath one no-follow root with regular/single-link, mode, size and
  stable-inode checks, and only for a period present in `pub_gazettes_v1`.

## Acceptance evidence

Mandatory full gate from `v2/`:

```text
Ruff format: 57 files formatted
Ruff lint: PASS
strict mypy: 57 source files PASS
pytest: 123 passed
contracts: 6 JSON schemas, 8 examples, 23 SQLite tables, 11 public API paths PASS
JavaScript syntax: PASS
frontend console smoke: PASS (empty/no-LLM route)
secret/Legacy isolation: 71 files, 3 synthetic fixtures PASS
production artifact: 45 runtime files
artifact SHA-256: cf21a9f00f6a8a55d372a3e78daff83662b137aa835f701a9fb50514ffd53aa9
```

Focused Stage 8 regressions cover OpenAPI schemas for every endpoint; pagination/cursor binding;
normal, empty and no-LLM DTOs; draft/path/secret leakage; direct SQL/PRAGMA/function denial;
malformed/duplicate/oversized inputs; search rate limiting; native/Legacy date invariants; atomic
pointer switch/reopen; SQLite byte stability and absence of sidecars; loopback HTTP; CSP/cache
headers; exact static/gazette routing; traversal; Node syntax; responsive/DOM-only frontend source.

Real imported-history acceptance used a fresh copy of
`/tmp/radar-stage6-historical-MONHtA/radar-v2-import.sqlite`, applied only migration `0002` to that
copy, installed it below a disposable active root and exercised the complete API matrix plus SPA
and static routes. Result:

```text
Evidence: /tmp/radar-stage8-historical-Dmob1G/acceptance.json
releaseId: rel_e404ff802c3e3c71083529ed
stateHash: ef5b4c3ef7ddfcda05c5aad331043bcc576ec641683e05d74ce1162e1e7c7f41
archive page: 74 issues
bounded material page: 100 materials
SPA: 200
static asset: 200
missing asset: 404
original historical SHA-256: e285e439df3ebaef777b35e7e26b1a49c89a99f5ce8a0db7988310a6af906f1c
```

Earlier failed historical copies were retained under `/tmp/radar-stage8-historical-xtDbjW` and
`/tmp/radar-stage8-historical-1DeZtx`; the earlier successful pre-manifest-view evidence also remains
under `/tmp/radar-stage8-historical-HQGVua`. Nothing was deleted.

## Legacy non-regression

After the final Stage 8 gate:

- `data/db/radar.sqlite` SHA-256 remained
  `481d5d6c9b54a58f78f288fb29c0eb072d43e74d6c2db8b14044a3153cd8f7f7`;
- `radar-api.service` and `caddy.service` were active with `NRestarts=0`;
- Legacy `http://127.0.0.1:8765/api/health` returned `ok`;
- no service, cron, Caddy, DNS, Local Ru or production pointer was changed.

## Residual boundary

Stage 8 is runnable only against an explicit local/disposable content root. It does not package an
application release, install systemd/Caddy configuration, publish gazette artifacts, or deploy.
Those actions remain Stage 9+ and application deployment stays manual-approved.
