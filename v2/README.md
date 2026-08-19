# Radar V2 through Stage 3

This directory is the isolated Radar V2 application workspace. Stage 3 adds the contract-complete
SQLite schema, deterministic migration and hashing layers, bootstrap-only Legacy importer,
published-only views/FTS, full projection equivalence tooling and an inter-process locked external
publisher audit journal. Normal V2 runtime access to Legacy remains disabled. No production host,
cron or service integration is included.

## Stack

- CPython 3.12 with the Stage 1 SQLite 3.45.1 build profile;
- `uv` locked environment;
- Ruff formatting/lint, strict mypy and pytest;
- dependency-free HTML/CSS/browser ES module, syntax-checked by Node 22;
- deterministic, allowlist-only production artifact.

The Python runtime has no third-party dependencies. Schema-validation and quality tools belong to
the locked development group and do not enter the production artifact.

## Layout

- `apps/api/` — inert API application identity and runtime-profile preflight;
- `apps/web/` — dependency-free static web shell;
- `packages/storage/` — strict SQLite schema, migration runner, FTS and canonical hashing;
- `packages/legacy_bridge/` — explicit bootstrap-only, read-only Legacy importer;
- `packages/publisher/` — external append-only audit journal; Stage 7 publication is still absent;
- `fixtures/synthetic/` — explicitly marked synthetic data only;
- `tests/` — skeleton, runtime-profile and isolation tests;
- `tools/` — isolation/secret scanner and deterministic artifact builder;
- `scripts/verify.sh` — the mandatory local/CI verification entrypoint.

## Verify

From the repository root:

```bash
./v2/scripts/verify.sh
```

The entrypoint performs locked sync, format check, lint, strict type checking, tests, the parent
Stage 1 contract validator, JavaScript syntax checks, isolation/secret scanning and a two-build
determinism plus manifest audit. Generated output is written to ignored `v2/dist/`.

For a disposable bootstrap import, use `python -m tools.legacy_import`; for replica comparison, use
`python -m tools.compare_databases`. Both commands require explicit paths and the importer requires
an explicit evidence-manifest hash and import timestamp.
