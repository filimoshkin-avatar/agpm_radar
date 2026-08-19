# ADR-0003: Python stack and isolated Radar V2 skeleton

Date: 2026-08-19

Status: accepted

## Context

Stage 2 needs a reproducible application workspace that can evolve independently while Legacy
continues to operate unchanged. The workspace must enforce the accepted Stage 1 contracts, pin the
same SQLite build, provide a dependency-free public web shell and produce an auditable production
artifact without development or editorial state.

This ADR selects the implementation stack and repository/build boundary only. It does not implement
the Stage 3 database/importer, Stage 5 candidate builder, Stage 6 validators/renderers, Stage 7
publisher/delta engine or Stage 8 public API.

## Decision

1. Radar V2 lives under the repository-local `v2/` boundary. It has no path dependency, import,
   symlink or runtime connection to Legacy application directories, databases, cron, deployment
   configuration, production services or Local Ru.
2. The application baseline is CPython 3.12. The runtime code is standard-library-only in Stage 2.
   `uv.lock` is authoritative; local and CI verification begin with `uv sync --locked`.
3. Ruff is the formatter/linter, mypy runs in strict mode and pytest is the test runner. JSON Schema
   and YAML libraries are development-only dependencies used to execute the parent Stage 1
   validator. The lock retains the validator's proven `jsonschema 4.10.3` resolver compatibility;
   upgrading its deprecated resolver is a separate contract-tooling decision. None is copied to the
   production artifact.
4. The accepted SQLite profile is pinned in code and tested before data work:
   - SQLite `3.45.1`;
   - source id `2024-01-30 16:01:20
     e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257ccalt1`;
   - compile options `ENABLE_FTS5` and `THREADSAFE=1`;
   - application id `RAD2` (`1380009010`) and user version `1`.
5. `apps/web/` is static HTML/CSS plus a browser-native ES module. It has no package manager,
   bundler, external imports or remote assets. Node 22 performs syntax checking only.
6. The initial Python namespaces are `apps/api` and the `contracts`, `domain`, `storage`,
   `publisher`, `delta`, `renderers`, `validation` and `legacy_bridge` package boundaries. Except for
   application identity and SQLite build preflight, these remain explicit inert skeletons until
   their planned stages.
7. Fixtures are inspectable JSON and must declare both `fixtureKind: synthetic` and
   `containsProductionData: false`. Production-derived data is forbidden in `v2/`.
8. The production artifact is constructed from an explicit runtime allowlist, normalized to epoch
   zero/mode `0644`, hashed with SHA-256 and rendered twice byte-for-byte. Its manifest lists every
   runtime path, byte size, mode and digest. Tests, fixtures, docs, build tools, lock/dev metadata,
   OpenClaw material, credentials, databases and raw corpus are structurally excluded.
9. `v2/scripts/verify.sh` is the single local/CI entrypoint. The root workflow is path-scoped to V2
   and the accepted contract family/validator; it never invokes Legacy build or deploy actions.

## Consequences

- A clean Stage 2 checkout has one command for the complete reproducibility and isolation gate.
- The runtime artifact is small and independently inspectable, but it intentionally is not yet a
  deployable public service.
- A package or tool added later must be represented in the lock, scanners and artifact allowlist as
  appropriate.
- Stage 3 is the first stage allowed to add schema/migration/importer behavior, and it must retain
  the isolation, synthetic-fixture and exact SQLite gates established here.
