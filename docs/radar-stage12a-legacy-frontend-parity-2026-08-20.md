# Radar V2 Stage 12A — Legacy frontend parity

Date: 2026-08-20
Status: implementation and local acceptance complete; production activation evidence is appended
after immutable release deployment.

## Corrected target contract

`radar.aipractice.space` remains the independent Legacy production system maintained through
Project Manager. `radar.agpm.space` retains the V2 backend, database and immutable release model,
but its initial frontend is the current Legacy visual/behavioral baseline. Future V2 frontend and
backend work is performed by coding agents through Git, tests and immutable releases. Legacy
changes do not propagate automatically.

The superseded independent V2 frontend remains in the previously active immutable application
release and is not deleted.

## Baseline and implementation

The Stage 12A source baseline was the live Legacy web root `work/radar-app` before any Stage 12A
change:

- `index.html`: SHA-256 `5bc8d09765e2957361c6ab2687f10081dc40976a624ccd9b8f4755f1137d29cf`;
- `styles.css`: SHA-256 `4e322a1a4c0b68b0cc8e7f18cc4468f705e603a59753d4d73a34fdff03ccffe4`;
- `app.js`: SHA-256 `31654e87ce46e5f6d211c8f4a3a7c3898bfab229f51d3d787392363e2b85c087`;
- favicon: SHA-256 `4757342b86258c1fd7f9e08c4bc66b5e6af3014d5c6ab4b8ca1a4914524e7b38`;
- social preview: SHA-256 `1805d2711f4f7a4dd6118afc9900a314472383ace8ad9c0c98c26281f0c2b430`;
- Golos Text font: SHA-256
  `17bb58fb69aec2dfb047a2ebf52534023e9b688c97a6b7ac795b0a72912c2063`;
- PT Mono font: SHA-256
  `cbe732b3b8fd211fd986ebdfc9b870ddeca4faab0bb5425fc509b37f9b4ac804`.
- Legacy gazette HTML: SHA-256
  `1e6ba2bb055a2821bca2e05ad7ef6ec57e3a558049875ffc5e601c58911b637d`.

Legacy HTML structure, CSS, widgets, responsive breakpoints and interaction implementation were
copied as the baseline. The deliberate V2 differences are limited to:

- canonical/social/footer host `radar.agpm.space`;
- immutable V2 static asset and gazette routes;
- a browser-side DTO adapter from the frozen V2 published API to the Legacy view model;
- HTTP(S)-only external URL validation;
- V2 CSP/static routing for the two exact font assets.

The browser does not call the Legacy hostname or Legacy API. The V2 release contains no Legacy
SQLite, runtime, pipeline or host path.

## Local acceptance

- Ruff format: PASS (72 files);
- Ruff lint: PASS;
- strict mypy: PASS (72 source files);
- pytest: PASS (145 tests);
- Legacy-parity empty/fallback console smoke: PASS;
- JavaScript syntax: PASS;
- secret/Legacy isolation scan: PASS (92 files, three synthetic fixtures);
- deterministic public production artifact: PASS, 26 files, SHA-256
  `9cb0f5e96cbebc0513737a13f7ced04fab69644a31d75964b2c24e68594306bb`;
- `git diff --check`: PASS.

## GRACE delta

GRACE-Delta: skip — the Radar repository has no GRACE module metadata or canonical design.md
contract. This report records the affected frontend/API-static/deployment/test files, source of
truth, invariants and verification gates instead.

## Production acceptance

Pending immutable release build, Local Ru activation, desktop/mobile browser comparison, public
API/static/CSP checks, service non-regression and rollback proof.
