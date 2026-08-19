# Radar V2 Stage 2: isolated repository skeleton evidence

Date: 2026-08-19

Status: completed

Base commit: `54d71066c24555b935cd737fe90923531ce8b60c`

## Delivered boundary

- `v2/` is a self-contained CPython 3.12 workspace with `pyproject.toml` and `uv.lock`.
- Required application boundaries exist at `apps/api` and `apps/web`.
- Required package boundaries exist for `contracts`, `domain`, `storage`, `publisher`, `delta`,
  `renderers`, `validation` and `legacy_bridge`.
- Python runtime code has no third-party dependencies. The web shell is static HTML/CSS plus one
  browser-native ES module with no npm tree, external import, CDN or remote asset.
- The Stage 1 SQLite build contract is pinned and tested: runtime `3.45.1`, exact source id,
  `ENABLE_FTS5`, `THREADSAFE=1`, `RAD2` application id and user version `1`.
- The sole fixture declares `fixtureKind: synthetic` and `containsProductionData: false` and uses
  only fabricated application identity.
- The parent Stage 1 validator's known-working `jsonschema 4.10.3` resolver compatibility is pinned
  in the dev lock. This dependency and every quality tool are excluded from runtime output.

Stack rationale and consequences are accepted in
`docs/adr/0003-radar-v2-python-stack-and-isolated-skeleton.md`.

## Mandatory local/CI gate

The single entrypoint is:

```bash
./v2/scripts/verify.sh
```

It ran to completion on CPython 3.12.3, uv 0.11.14 and Node 22.23.2:

```text
[verify] locked Python 3.12 development sync
Created a fresh temporary virtual environment; resolved 16 packages; installed 14 packages

[verify] Ruff format check
19 files already formatted

[verify] Ruff lint
All checks passed!

[verify] strict mypy
Success: no issues found in 18 source files

[verify] pytest
16 passed

[verify] parent Stage 1 contract validator
Radar V2 contracts validation: PASS
JSON schemas: 6
Examples: 8
SQLite tables: 23
Public API paths: 11

[verify] dependency-free web ES module syntax
JavaScript syntax: PASS (1 module)

[verify] secret and Legacy-isolation scan
Radar V2 secret/isolation scan: PASS
Files scanned: 28
Synthetic fixtures: 1
Runtime imports: Python stdlib/local only; browser module dependency-free

[verify] deterministic production artifact and manifest
Radar V2 production artifact: PASS
Runtime files: 16
Artifact SHA-256: 36584192d438060fb849a40807bebbfaa29cecbd44f6fe3f1847fbdfc81ee443

Radar V2 Stage 2 verification: PASS
```

The root workflow `.github/workflows/radar-v2.yml` is read-only and path-scoped to `v2/`,
`contracts/`, their parent validator and the workflow itself. It runs no Legacy build, deploy or
production action.

## Production artifact audit

`v2/tools/build_production_artifact.py --check` performs two independent in-memory renders and
requires byte equality. Tar members are sorted and regular-file-only; mtime, uid/gid and mode are
normalized. `MANIFEST.json` records the path, byte size, `0644` mode and SHA-256 of every runtime
file, and the builder reopens the gzip/tar stream and verifies membership plus content.

The allowlist contains only:

- the Python application entrypoint;
- the dependency-free static web shell;
- the required Python package skeletons and SQLite profile.

The artifact contains no `tests`, `tools`, fixtures, docs, virtual environment, pyproject/lock dev
metadata, OpenClaw material, credentials, databases, SQLite sidecars, migrations or raw corpus. The
generated artifact and caches remain ignored local verification output under `v2/dist/` and
`v2/.venv/`; they are not repository inputs.

## Isolation audit

Final path/diff checks show no modifications under `backend/`, `pipeline/`, `work/`, `deploy/`,
`contracts/` or `tools/contracts/`. The scanner rejects symlinks, secret-shaped content, credential
filenames, DB files, raw/corpus paths, non-synthetic fixtures, Legacy/production path fragments,
third-party Python runtime imports and external browser dependencies.

No Legacy runtime/config/data/cron, `/etc`, services, DNS, Local Ru or production state was mutated,
connected to or invoked. No application release or deployment was attempted.

## Next step

Stage 3 may implement the V2 SQLite schema and Legacy importer against the frozen Stage 0/1 evidence.
It must preserve this isolation gate and begin with synthetic importer tests; Stage 2 deliberately
contains no schema, migration runner, importer data access, publisher, delta engine or public API.
