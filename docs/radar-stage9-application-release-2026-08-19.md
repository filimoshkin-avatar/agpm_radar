# Radar V2 Stage 9 — application release automation

Date: 2026-08-19
Status: accepted locally; no production or Local Ru activation

## Scope and invariants

Stage 9 implements the manual-approved application-release boundary from a completely clean Git
commit. It packages the public API, web frontend and versioned migrations as separate immutable
role artifacts inside one canonical release envelope. The release manifest binds Git provenance,
contract compatibility, the exact SQLite runtime profile and every artifact hash.

This stage did not install the included inert systemd/Caddy templates, create Local Ru users or
paths, restart services, change cron/Caddy/DNS, connect to Local Ru, or activate a real V2 database.
The acceptance approval was explicitly test-only and applied only to retained private `/tmp`
staging roots. Legacy remained the production contour throughout.

## Implemented release boundary

- `build_application_release.py` refuses a dirty worktree, a non-HEAD commit, a mismatched tag or
  any untracked artifact input. A second in-memory build must be byte-identical before output is
  written.
- The outer application package and the API/web/migration role archives use canonical gzip/tar
  bytes, exact ordered membership, normalized metadata and explicit SHA-256 manifests. Readers
  reject traversal, links, special files, noncanonical bytes, unexpected members, unsafe modes,
  size/member-count excess, provenance rebinding and nested artifact tampering.
- `compatibility-manifest.json` freezes the application release ID, commit, schema/table/API and
  candidate/delta/result/gazette contract versions, exact SQLite version/source ID/compile options,
  creation time and the three role hashes.
- The public API artifact contains only the published read path and shared contract/storage code;
  it excludes candidate builders, publisher, editorial/LLM orchestration, deployment tools,
  fixtures, tests, secrets and OpenClaw integration. The web archive is dependency-free. The
  migration runner is a separate staging-only artifact.
- The migration runner accepts only a private regular single-link database copy and immutable
  migration bundle, validates the exact SQLite runtime, applies ordered versioned migrations, and
  proves integrity, foreign keys, schema/compatibility hashes and unchanged logical content state.
- Source and production copies are migrated independently with the same bundle. Activation order
  is content pointer, API symlink, web symlink and smoke for source, then the same sequence for
  production. Rollback is dependency-ordered in reverse and must restore/prove both targets.
- Application deploy and Stage 7 content publishing share one no-follow `radar-mutation.lock`.
  A content candidate still cannot carry SQL, DDL, migrations or application-owned metadata.
- Approval is an exact tuple of approval ID, application release ID, Git commit and package hash.
  A release ID cannot later be rebound to different bytes or provenance.
- Release directories and databases remain immutable and retained. Atomic pointer/symlink swaps
  are pinned and rechecked; no in-place application or active-database overwrite exists.

## Adversarial regressions

The focused Stage 9 suite contains 18 tests covering:

- deterministic role and outer artifacts plus strict compatibility-manifest validation;
- public-runtime minimization and publisher/editorial/LLM exclusion;
- outer/inner tampering, traversal, symlink, hardlink, special/mode and noncanonical archives;
- provenance source-tree digest rebinding, dirty Git, wrong HEAD/tag and untracked inputs;
- identical `0001 -> 0002` migration on both staging databases with content-state preservation;
- manual-approval mismatch, shared-lock exclusion and overlapping target-root rejection;
- rollback after each of eight activation/smoke fault points;
- pre-activation source/production metadata-parity rejection;
- release-ID rebinding, timestamp/runtime drift and migration-runner link rejection.

## Clean-commit release evidence

Implementation commit:

```text
d45069d8639019da02bfb7927484d32d7c327331
```

The release builder ran with a completely clean worktree and rebuilt the package twice:

```text
Application release: app_release_20260819_d45069d
Package: /tmp/radar-stage9-build-YUHK8B/radar-v2-application-release.tar.gz
Package bytes: 68953
Package SHA-256: 81b7c26802c6f82e23ae8f502366405a610b810eb4c8c498a3dc630a882eee78
Provenance sourceTreeSha256: 7eba7bd904917df712e79708aa594e38f3faab48d56998b28ff8c194559e58a9
```

Role artifacts:

| Kind | Bytes | SHA-256 |
|---|---:|---|
| API | 30,776 | `c807e9208aa811a0bb47b3341ebf4a4f4f4ff7dd628f7911cc62e03d6680c0e3` |
| migrations | 27,912 | `33cbcf0ba492cd2429799b1fe57ea0ce44d1acd7ea8a3b0973eab0c2d369c8f7` |
| web | 8,092 | `d96e5e30346d641bc5ee8d672b6ef2380f875d189b28c86454321c5669ff65d2` |

All retained build files are private single-link `0600` files.

## Local deployment and rollback rehearsal

The clean-commit package was applied to two independent private targets using a test-only approval.
Both began from the same pre-Stage 9 SQLite database, independently applied migrations `0001` and
`0002`, completed all eight activation/smoke steps, rolled back in reverse dependency order, proved
the original pointers, and then repeated the complete activation successfully.

```text
Evidence: /tmp/radar-stage9-evidence-parent-kpwCTJ/rehearsal/acceptance.json
approvalId: stage9-local-rehearsal
testOnlyApproval: true
source content release: content_release_stage9_acceptance
production content release: content_release_stage9_acceptance
source schema SHA-256: 5c7e6e66afc7fd814f25c5bb7b441e22131db8ffc35cf00fd2d81760ccbc6266
production schema SHA-256: 5c7e6e66afc7fd814f25c5bb7b441e22131db8ffc35cf00fd2d81760ccbc6266
logical state SHA-256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
rollbackProven: true
```

Every replicated table count/hash is identical between the two accepted databases. The baseline
database remained byte-identical. The generated release, previous releases, final releases,
pointers, databases, audit material and acceptance JSON are retained; no cleanup was performed.

## Mandatory full gate

Final verification from the repository root:

```text
Ruff format: 71 files formatted
Ruff lint: PASS
strict mypy: 71 source files PASS
pytest: 142 passed
contracts: 6 JSON schemas, 8 examples, 23 SQLite tables, 11 public API paths PASS
JavaScript syntax: PASS
frontend console smoke: PASS (empty/no-LLM route)
secret/Legacy isolation: 87 files, 3 synthetic fixtures PASS
public production artifact: 21 runtime files
public artifact SHA-256: 07bdfd832ad88e7618db5b2fc1df64830f7bf32625db27d0d91c16feacdbf572
```

## Legacy non-regression

After the final gate and local rehearsal:

- `data/db/radar.sqlite` SHA-256 remained
  `481d5d6c9b54a58f78f288fb29c0eb072d43e74d6c2db8b14044a3153cd8f7f7`;
- `radar-api.service` and `caddy.service` were active with `NRestarts=0`;
- Legacy `http://127.0.0.1:8765/api/health` returned `ok`;
- `pipeline/bin/radar_healthcheck.sh --production` passed for issue `2026-08-19` with three
  materials;
- no service, cron, Caddy, DNS, Local Ru or real production pointer was changed.

## Residual boundary

Stage 9 proves the release mechanism locally. Stage 10 begins with a read-only Local Ru capacity,
port, Caddy, UFW and NRD-load audit. Creating users/paths, installing units/templates or deploying
this release there remains a separate externally mutating step and requires explicit owner approval.
