# Radar V2 Stage 2 skeleton

This directory is the isolated Radar V2 application workspace. Stage 2 establishes only
repository boundaries, locked development gates, runtime build metadata and inert component
skeletons. It does not connect to Legacy runtime state, production hosts, cron, databases or raw
corpora.

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
- `packages/` — Stage 2 boundaries for contracts, domain, storage, publisher, delta, renderers,
  validation and Legacy bridge;
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

Stage 3 may implement SQLite and the importer only after this Stage 2 boundary remains green.
