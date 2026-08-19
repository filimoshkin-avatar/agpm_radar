# Radar V2 Stage 5 — candidate builder and Project Manager adapter

Date: 2026-08-19
Status: implemented, independently reviewed and accepted
Scope: V2 code, synthetic/disposable tests and documentation only

## Source of truth and boundary

Stage 5 implements the work and gate in `docs/migration-plan-review-2026-08-19.md`, using the frozen
Stage 1 candidate, SQLite, publisher-result and Project Manager report contracts. It consumes the
complete Stage 4 V2 snapshot identity/attestation. It does not implement Stage 6 renderers, Stage 7
publisher/deltas, Local Ru deployment, services, cron, Caddy, DNS or production content changes.

Radar has no GRACE `M-*`/`V-M-*` module map. The governed scope is therefore recorded as an explicit
GRACE skip: Stage 5 implementation, regressions and evidence only; Legacy and production paths are
unchanged.

## Implemented components

- dependency-free runtime validators and builders for accepted daily, correction and gazette
  manifests, including exact unknown-field rejection and LLM attempt/effective/fallback semantics;
- daily binding to snapshot id, manifest hash, payload hash, checksum identity, item count and the
  immutable V2 consumption attestation;
- correction binding to expected base plus exact issue-aggregate and shared-material row hashes;
- gazette binding to optimistic state, exact asset membership/bytes/hash/media type/entrypoint and
  UTF-8 text-asset scrubbing;
- complete typed full-row mutations with deterministic ordering, optimistic before-state hashes,
  canonical after-row hashes and affected-table counts;
- explicit current draft aggregate and editorial queue carry-forward, plus daily source snapshot;
- source SQLite opened read-only through a private, single-link pinned file descriptor;
- create-only staging SQLite under a pinned private directory, transactional replay, integrity/FK,
  FTS rebuild/parity, affected-table counts/hashes and final logical-state hash;
- atomic nested package publication with exact `0500` directories, `0400` single-link files,
  canonical JSON, exact membership, checksums, machine payloads and human preview;
- duplicate candidate-id and idempotency-key rejection before replay and again under the final
  package-store lock;
- Project Manager CLI playbooks for daily/correction/gazette/status/retry/report;
- strict publisher-result adapter preserving requested/attempted/effective LLM evidence and making
  fallback or total LLM outage owner-visible.

Project Manager candidate mutations cannot author `content_releases`, schema/application metadata,
migrations, arbitrary SQL/DDL or derived FTS rows. `status` and `retry` verify immutable package
bytes only; no Stage 5 code has publication, network, service, cron or DNS capability.

## Package layout

```text
<candidate-id>/
  manifest.json
  checksums.sha256
  preview.txt
  payload/
    package-metadata.json
    replication-mutations.json
    issue.json | gazette.json
    materials.json
    analyses.json
    stats.json
    queue-changes.json
    snapshot-attestation.json   # daily only
  assets/                       # gazette only
```

The frozen Stage 1 candidate manifest rejects unknown fields, so completeness counts and the
staging result are carried in the checksummed `payload/package-metadata.json`; the complete typed
declaration remains in `payload/replication-mutations.json`.

## Focused acceptance

The Stage 5 suite builds and replays all three operations on newly created synthetic SQLite files.
It checks deterministic package bytes, replay idempotency, public exclusion of drafts, preservation
of drafts/queues/snapshot evidence, correction replacement, gazette assets, CLI output and final PM
reporting.

Negative regressions reject:

- unknown fields, schema mismatch, contradictory LLM state and malformed publisher results;
- SQL/DDL, `content_releases`, non-finite JSON, invalid numeric minima, secret-shaped text and host
  paths in manifests, mutations, assets or owner-visible reports;
- absolute/traversal paths, parent symlinks, staging symlinks and source/package hardlinks;
- package/attestation mode changes, package membership/checksum/view drift and duplicate ids/keys;
- source base/release/state drift, issue/material precondition drift and unsafe staging collisions.

The adversarial review found and closed three boundary issues before acceptance:

1. An inferred `stats.viewed == snapshot.itemCount` rule rejected an accepted frozen fixture. The
   unsupported rule was removed; contract/examples remain authoritative.
2. The generic table map initially made `content_releases` structurally representable. Candidate
   validation now rejects it explicitly, matching the Stage 1 Project Manager restriction.
3. Traversal and parent-symlink attempts were blocked but exposed low-level storage/kernel errors.
   Domain boundaries now return stable candidate/mutation/staging errors, with regressions.

## Mandatory verification

Command from repository root:

```bash
./v2/scripts/verify.sh
```

Accepted output:

```text
[verify] Ruff format check
41 files already formatted

[verify] Ruff lint
All checks passed!

[verify] strict mypy
Success: no issues found in 41 source files

[verify] pytest
73 passed in 2.86s

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
Files scanned: 52
Synthetic fixtures: 2
Runtime imports: Python stdlib/local only; browser module dependency-free

[verify] deterministic production artifact and manifest
Radar V2 production artifact: PASS
Runtime files: 31
Artifact SHA-256: c35bf9918329b0d635097935766bc79ca7c714566962e8fccfeedd559ea43f0d

Radar V2 verification: PASS
```

`git diff --check` also passed. The production manifest contains the candidate-builder CLI and all
new domain/storage/adapter modules; it excludes tests, fixtures, documentation, SQLite data and
credentials. Runtime dependency count remains zero.

## Legacy production non-regression

After the accepted Stage 5 gate:

- `data/db/radar.sqlite` SHA-256 remained
  `481d5d6c9b54a58f78f288fb29c0eb072d43e74d6c2db8b14044a3153cd8f7f7`;
- `radar-api.service` and `caddy.service` were active with `NRestarts=0`;
- local `/api/health` returned `ok` against the same Legacy database;
- `pipeline/bin/radar_healthcheck.sh --production` passed with latest issue `2026-08-19` and three
  materials.

No service, production database, cron, Caddy, DNS or Local Ru state was changed. No files were
deleted.

## Residual risks and next boundary

- Stage 5 packages are desired-state inputs, not publishable releases. They intentionally lack
  renderers, delta/result generation, activation, transport and rollback authority.
- Pinned descriptors, exact permissions and mutation detection protect the code-level boundary;
  later deployment still needs separate OS service identities if hostile same-UID code is in scope.
- Operator staging SQLite is retained for inspection/audit. Later publisher stages must define its
  lifecycle without weakening create-only and reconciliation rules.
- Real daily cron remains unchanged until the planned end-to-end and dual-run stages.

The next sequential step is Stage 6: deterministic renderers and validators. Work stops before it.
