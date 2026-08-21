# Radar V2 production system through Stage 16

This directory is the isolated Radar V2 application workspace. Stages 3 and 4 established the
contract SQLite/importer boundary plus the immutable Legacy/V2 snapshot fork. Stage 5 adds closed
candidate packages and the Project Manager adapter. Stage 6 adds an explicit published-only public
DTO projection, canonical JSON, deterministic dependency-free DOCX, no-LLM rendering, database and
artifact invariants, and immutable gazette validation. Stage 7 adds exact full seeds, typed
all-table deltas, transactional staging apply and a durable two-root local publisher simulation.
Stage 8 adds a pointer-aware published-only API, strict SQLite authorizer, bounded public DTO
queries, loopback HTTP transport and responsive same-origin frontend with gazette routing.
Stage 9 adds clean-commit provenance, role-separated immutable application artifacts, a frozen
compatibility manifest, staging-only migrations, atomic activation and coordinated rollback.
Stage 10 installs the accepted application and a separately verified exact runtime on Local Ru,
activates only an empty schema release, and runs the hardened API on loopback. Stage 11 builds and
activates a deterministic Legacy-derived full seed, proves all-table and historical API parity,
private-state filtering, correction and content rollback/re-activation. Public Caddy/DNS,
publisher transport and cron remain deliberately absent.
Stage 12 established public frontend parity and the independent public contour. Stages 13–14 add
the restricted remote activation boundary and production publication workflow. Stage 15 runs the
post-Legacy daily publication/comparison, and Stage 16 closes operational acceptance, rollback and
disaster-recovery evidence. The public V2 contour is `https://radar.agpm.space`; Legacy remains an
independent production system.

## Stack

- CPython 3.12 with the Stage 1 SQLite 3.45.1 build profile;
- `uv` locked environment;
- Ruff formatting/lint, strict mypy and pytest;
- dependency-free HTML/CSS/browser ES module, syntax-checked by Node 22;
- deterministic, allowlist-only production artifact.

The Python runtime has no third-party dependencies. Schema-validation and quality tools belong to
the locked development group and do not enter the production artifact.

## Layout

- `apps/api/` — pointer-aware read-only API, published DTO repository and loopback HTTP/static app;
- `apps/candidate_builder/` — explicit daily/correction/gazette/status/retry/report CLI;
- `apps/web/` — responsive dependency-free latest/archive/search/gazette frontend;
- `packages/storage/` — strict SQLite schema, migration runner, FTS and canonical hashing;
- `packages/storage/safe_files.py` — no-follow, private, fsynced, atomic no-replace artifacts;
- `packages/domain/snapshot.py` — canonical four-file snapshot creation and consumption checks;
- `packages/domain/dual_run.py` — separate branch workspaces, attestations and daily comparison;
- `packages/domain/candidates.py` — closed contract-v1 candidate builders/runtime validation;
- `packages/domain/candidate_mutations.py` — database-derived typed desired-state mutations;
- `packages/domain/candidate_package.py` — replayed, previewed and immutable candidate packages;
- `packages/renderers/` — canonical public JSON and byte-stable daily DOCX generation;
- `packages/validation/` — published DTO, JSON/DOCX and gazette fail-closed validators;
- `packages/delta/` — exact full seeds, typed row deltas and transactional create-only apply;
- `packages/legacy_bridge/` — explicit bootstrap-only, read-only Legacy importer;
- `packages/publisher/` — audit/result adapters, durable state machine and local activation/rollback
  simulation;
- `packages/deployment/` — canonical application artifacts, compatibility manifest, versioned
  staging migrations and manual-approved two-target activation/rollback;
- `apps/migration_runner/` — standalone staging-only migration entrypoint;
- `deploy/templates/` — inert hardened systemd/Caddy templates for later approved deployment;
- `fixtures/synthetic/` — explicitly marked synthetic data only;
- `tests/` — contract, runtime, publication, API/frontend, security and isolation tests;
- `tools/` — isolation/secret scanner and deterministic artifact builder;
- `scripts/verify.sh` — the mandatory local/CI verification entrypoint.

## Operator entrypoints

The project intentionally remains non-installable (`[tool.uv] package = false`). Approved module
entrypoints therefore run with `PYTHONPATH` set to this directory. Shell launchers resolve their
own absolute root and export it before invoking `.venv/bin/python`; systemd units use an explicit
`WorkingDirectory`. Tests exercise the supported `-m` entrypoints from a foreign working directory.

The daily Stage 15 launcher additionally:

- fails closed if the three Git-owned Legacy incident fixes differ from the Project Manager runtime
  mirror;
- waits for the requested Legacy issue, then selects the oldest unfinished issue from a bounded
  seven-day catch-up window;
- retains every failed attempt under `attempt-NNN` while treating `combined-report.json` as the
  sole completion marker;
- retries the public issue endpoint, reads application compatibility from the active content DB,
  and exits non-zero when URL differences are not explained by recorded V2 exclusions.

`PublishInputs.finished_at` and `duration_ms` stay stable because they participate in publication
idempotency. `combined-report.json.generatedAt` is the actual report completion time.

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

## Stage 5 boundary

The candidate builder accepts only the frozen contract-v1 manifest. It derives full-row mutations
from a private, single-link source SQLite opened through a pinned file descriptor; the caller never
supplies SQL. Every mutation has an optimistic row precondition, full typed values, canonical row
hash and deterministic sequence. The completeness declaration is bound to exact affected-table
counts. Project Manager candidates cannot author `content_releases`, migrations, schema metadata or
derived FTS rows.

Daily candidates must reproduce the exact Stage 4 V2 snapshot identity and immutable consumption
attestation. Correction candidates must match the current issue-aggregate hash and shared-material
preconditions. Gazette candidates must match every asset byte/hash and pass secret/path/executable
scrubbing. Current drafts and editorial queue rows are carried explicitly, replayed into a newly
reserved disposable staging database, and checked through integrity, foreign keys, FTS parity and
logical-state hashing.

Packages are published atomically as nested `0500` directories with `0400` single-link files,
canonical JSON, exact membership and checksums. `status` and `retry` only re-verify immutable bytes;
they do not publish. The Project Manager adapter preserves requested/attempted/effective LLM
semantics and emits owner-visible fallback or complete-outage warnings.

## Stage 6 boundary

The public issue projector reads only a published issue aggregate from the contract SQLite and
constructs the frozen `IssueDetail` DTO explicitly. It rejects draft leakage, inconsistent stats,
non-contiguous or duplicate materials, invalid publication dates, sparse required rows, host paths
and secret-shaped values. Historical `legacy_inferred` date-only material timestamps are normalized
at this compatibility boundary; native V2 rows remain strict second-precision UTC.

JSON is canonical UTF-8 with sorted keys and a final newline. DOCX is built without third-party
runtime dependencies, with a fixed member allowlist, fixed ZIP metadata, normalized modes, explicit
HTTP(S) hyperlinks and bounded XML. Both are validated independently against the same public DTO.
When LLM output is unavailable or sparse in a historical import, the renderer emits an explicit
deterministic fallback and never claims primary-model success.

Gazette validation accepts only the candidate-declared immutable asset set and verifies exact byte
counts/hashes, a local relative entrypoint, bounded safe HTML/CSS/SVG, local references and the
absence of scripts, active content, traversal, remote imports, secrets and host-local paths. These
renderers and validators still do not activate a database, publish files or mutate production;
those production capabilities remain reserved for later deployment stages.

## Stage 7 boundary

The full-seed path exports and imports the exact verified SQLite bytes plus a canonical manifest
covering release identity, schema, file SHA-256 and counts/hashes for all 23 replicated tables. The
source inode is opened once without symlink traversal; byte copying and immutable read-only SQLite
inspection stay bound to that descriptor and recheck mode, link count, size and timestamps. Import
always creates a new private inode, reopens it and proves the complete manifest again.

The delta engine compares two verified release databases and emits only contract-allowlisted typed
row inserts/upserts/tombstones followed by one publisher-owned `content_releases` marker. Every row
has an optimistic precondition and canonical after-hash; the envelope fences base release,
sequence, schema and logical state and declares before/after counts plus hashes for every table.
Apply copies the pinned base into a newly reserved staging path, runs one transaction, rebuilds and
validates derived FTS state, then reopens the sealed path. Missing/out-of-order releases, stale row
hashes, application-owned table changes, SQL/DDL-shaped values and unsafe assets fail closed.

`LocalPublisherSimulator` exercises the frozen publisher state machine against separate private
source and disposable-production release roots. It serializes through `radar_mutation`, retains
an immutable canonical delta/LLM/issue-date input for each candidate, retains immutable release
databases, atomically replaces only `active.json`, verifies release/state after reopen, commits
source only after the disposable public check, and restores the exact previous pointer on
post-activation failure. If rollback cannot be proven, the durable journal blocks new candidates.
Retries must present the same exact input and recover crashes before activation, after activation,
between result save and `SUCCEEDED`, and between rollback state and result persistence; an already
completed candidate is replayed without reapplying its delta.

This is deliberately a local simulation boundary. Stage 7 does not install a service, connect to
Local Ru, change Legacy, schedule cron, edit Caddy/DNS or activate any real production database.

## Stage 8 boundary

The public API follows the frozen OpenAPI contract and reads only published views. It pins the
immutable database selected by the atomic content pointer, proves release/schema/state markers and
reopens after a switch. SQLite `mode=ro`, `immutable`, `query_only` and an authorizer independently
deny internal tables, writes, PRAGMA and unapproved functions. Query/cursor/response/search limits
are explicit and public errors never contain exception, SQL or host-path details.

The same-origin frontend renders latest/history/search/gazette indexes, empty issues and LLM
outages without HTML injection or remote dependencies. SPA, exact static assets and gazette paths
have separate routing and CSP/cache policy; missing or traversal paths are 404. A loopback transport
smoke and headless DOM console smoke are part of the mandatory gate.

Stage 8 remains local/disposable. It installs no application release or service and changes no
production pointer, cron, Caddy, DNS or Local Ru state. Application automation begins at Stage 9.

## Stage 9 boundary

An application release can be built only from a completely clean tracked `HEAD` (or an exact tag)
and contains separate canonical API, web and migration archives. The compatibility manifest binds
the commit, source-tree digest, schema/table/public API and package contract versions, exact SQLite
runtime profile and every role hash. Archive readers reject noncanonical bytes, unexpected members,
links, traversal, unsafe modes, bounds violations and provenance or nested-artifact tampering.

The migration runner operates only on an inactive private staging database and preserves logical
content state. Source and production copies receive the same ordered migration bundle, then content,
API and web pointers activate in dependency order with smokes. Any fault restores and proves both
previous targets before another deploy may proceed. Application deployment and content publishing
share the same `radar-mutation.lock`; candidates still cannot carry SQL, DDL or migrations.

Stage 9 acceptance used a clean commit and test-only approval against two retained `/tmp` roots,
proved rollback and re-activation, and did not install the inert templates or touch Legacy, cron,
Caddy, DNS, Local Ru or a real production pointer. Those external changes start only after the
read-only Stage 10 audit and explicit owner approval.

## Stage 10 boundary

After owner approval, Local Ru received locked `radar-v2-api` and `radar-v2-deploy` identities,
private incoming/audit/data paths, the exact accepted application release, and a reproducibly built
relocatable CPython 3.12.3 runtime carrying the exact SQLite 3.45.1 profile. The system Python and
SQLite were not changed. The API runs non-root from immutable versioned targets, binds only
`127.0.0.1:8765`, has zero capabilities plus strict systemd sandboxing, and reads a schema-only
content release through an `EROFS` service mount.

The empty Local Ru database and independent local staging database are byte-identical after the
same migrations and contain no Legacy domain rows. Exact runtime/application membership, API
semantics, security headers, systemd score, external-port closure and NRD/Caddy/UFW/Legacy
non-regression are retained in `docs/radar-stage10-local-ru-loopback-2026-08-20.md`.

Stage 10 installs no public Radar vhost, changes no DNS, imports no history, activates no publisher
transport and changes no cron. Full seed and historical endpoint parity begin only at Stage 11
under a separate data-transfer approval.

## Stage 11 boundary

After owner approval, frozen Legacy inputs were imported into release
`rel_e404ff802c3e3c71083529ed` and exported as a byte-stable full seed. Source, round-trip replica
and Local Ru match on release/state plus counts and logical hashes for every one of the 23
replicated tables. The retained production SQLite includes 74 evidence-backed published issues,
254 issue/material relations, full Legacy provenance, 128 private queue rows, one exact snapshot
and one gazette release.

The loopback API matches Legacy history across all 74 dates while exposing none of the private
queue/unassigned material identifiers or internal fields. A disposable historical correction
proved both the normal publish path and forced-smoke rollback. The real Local Ru content pointer
was then rolled back to the empty Stage 10 release and reactivated without restarting the service;
release/state, immutable database hashes, UID/GID/mode/link-count and API reopen were verified at
each boundary.

The acceptance and retained evidence are documented in
`docs/radar-stage11-initial-seed-2026-08-20.md`. Stage 11 adds no Caddy vhost, DNS record, publisher
SSH transport, cron or public cutover. Those remain separately approved later stages, beginning
with the Stage 12 shadow hostname.

## Stage 14 source-side publisher boundary

`apps/publisher_runner` is the explicit manual Project Manager entrypoint for the restricted
Stage 13 transport. It verifies an immutable candidate package and its create-only staging DB,
finalizes a source release, builds the closed row delta, sends one canonical request through a
no-shell SSH argv, verifies the exact remote release/state result, installs the source release and
only then atomically commits the source pointer. Machine result and owner-facing report outputs are
create-only. A completed candidate replays its retained result without calling the transport or
applying the delta again.

The runner does not read or mutate Legacy, edit Project Manager cron, install a timer, deploy an
application release or begin dual-run. `tools/build_stage14_daily.py` is a manual acceptance input
adapter for a captured Legacy public response; it enforces the approved 30-day/unresolved material
window before the candidate package boundary and records excluded materials explicitly.
