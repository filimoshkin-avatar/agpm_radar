# Radar V2 through Stage 4

This directory is the isolated Radar V2 application workspace. In addition to the accepted Stage 3
SQLite/importer boundary, Stage 4 adds the post-collection immutable input snapshot, independent
Legacy/V2 branch copies and consumption attestations, capability-scoped branch state, isolated
runner outcomes and a canonical daily comparison report. Normal V2 runtime access to Legacy
remains disabled. V2 publication, candidate construction, production hosts, cron and service
integration are deliberately absent.

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
- `packages/storage/safe_files.py` — no-follow, private, fsynced, atomic no-replace artifacts;
- `packages/domain/snapshot.py` — canonical four-file snapshot creation and consumption checks;
- `packages/domain/dual_run.py` — separate branch workspaces, attestations and daily comparison;
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

## Stage 4 boundary

`create_snapshot` returns the complete creation identity: snapshot id plus SHA-256 of exact
`manifest.json`, exact `checksums.sha256` and the domain-separated aggregate of both payload files.
That returned identity is a mandatory input to `fork_snapshot`; therefore a fully rewritten but
internally self-consistent directory with the same snapshot id is still rejected.

Each branch receives its own inodes under `legacy/` or `v2/`, with separate `queues/`, `corpus/`,
`database/` and `logs/`. A canonical private attestation is written only after the branch copy has
been re-opened and verified. The same verification runs immediately before branch execution and
again before comparison. Snapshot files/directories must remain exactly `0400`/`0500`, reads stay
bound to the pinned directory descriptor, and execution/comparison must match the complete original
attestation rather than only its snapshot id/hash. V2 has no publication capability in Stage 4 and
any publication claim is rejected.

Legacy preservation is deliberately narrow and testable: only the snapshot pathname, private file
permissions, and branch-local attestation/log locations may differ. Output bytes and exit code, or
the exact exception type and message for a failure baseline, must match. Any successful-result drift
fails closed as `LegacyBaselineMismatch` while retaining the observed result for diagnosis. Runtime
branch objects validate types and LLM relationships instead of trusting annotations. V2 failure is
recorded independently and cannot replace the Legacy outcome.
