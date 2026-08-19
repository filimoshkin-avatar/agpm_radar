# Radar V2 Stage 4: immutable input snapshot and Legacy/V2 fork evidence

Date: 2026-08-19

Status: completed and independently accepted 2026-08-19

Base commit: `7ad6086fe54c66d8958698c88f4a7c049023d2f3`

## Scope and safety

Stage 4 changed only `v2/**`, the repository README, this evidence document and the
Stage 4/master-plan status. It did not
modify or invoke the Legacy runtime, production database, cron, services, Caddy, DNS, Local Ru,
deployment state or any production path. All functional evidence uses the explicitly synthetic
fixture `v2/fixtures/synthetic/stage4-collected-input.json` and disposable pytest workspaces. The
first independent adversarial findings are retained on the host at
`/tmp/radar-stage4-independent-GBVTZZ/findings.json`; no project or production files were deleted.

Stage 5 candidate construction is absent. No publisher, deployment adapter, network operation,
public root or V2 publication path was added. A Stage 4 V2 result must state
`publication_status=not_published`; any other value fails the branch contract.

The accepted Stage 1 schemas were not changed. The Stage 1 daily candidate intentionally carries
`manifestSha256` and aggregate `payloadSha256`; Stage 4 additionally binds the exact
`checksums.sha256` hash in both consumption attestations. Stage 5 can project the accepted candidate
fields later without weakening the complete Stage 4 consumption identity.

## Boundary and format

`packages/domain/snapshot.py` establishes the boundary immediately after common collection. It
creates exactly:

```text
<snapshot-id>/
  manifest.json
  candidates.jsonl
  safe-evidence-index.json
  checksums.sha256
```

All JSON uses NFC-normalized strings, lexically sorted keys, UTF-8, no insignificant whitespace,
no non-finite numbers and one final newline. JSONL preserves collected-item order while rendering
each object canonically. Manifest payload descriptors are path-sorted and bind exact byte length and
SHA-256. `checksums.sha256` is a canonical path-sorted digest list for the manifest and both payload
files.

The aggregate payload digest is domain-separated SHA-256 over, for each lexically sorted payload,
the 8-byte big-endian path length, UTF-8 path, 8-byte big-endian content length and exact content.
It therefore binds membership, paths, boundaries and bytes without concatenation ambiguity.

The complete `SnapshotIdentity` is:

- `snapshot_id`;
- SHA-256 of exact `manifest.json` bytes;
- SHA-256 of exact `checksums.sha256` bytes;
- aggregate payload SHA-256;
- item count.

The identity returned at creation is a mandatory argument to `fork_snapshot`. Verification thus
rejects both ordinary byte damage and a fully rewritten, internally self-consistent snapshot that
reuses the same snapshot id.

## Immutable consumption and filesystem safety

Creation renders in memory, writes a private staging directory, fsyncs every file and directory,
sets snapshot files/directories to `0400`/`0500`, then publishes through Linux
`renameat2(RENAME_NOREPLACE)`. Existing targets are never replaced. Concurrent same-id creation has
exactly one winner and leaves no temporary member.

Every path component is opened through directory FDs with `O_NOFOLLOW`; snapshot members are read
relative to the already pinned snapshot directory FD. A coordinated parent-path replacement cannot
redirect an in-progress verification to another directory. Regular-file type, single link, exact
immutable `0400` file mode, exact `0500` snapshot-directory mode, stable inode/size/mode/mtime/ctime
during read and exact directory membership are required. Symlink, hardlink, traversal,
absolute-path, special-file, extra-member, owner-write/broad-mode, truncated/noncanonical JSON and
checksum mismatches fail closed.

Verification runs after creation, independently before each branch copy, after each copy, from the
branch attestation immediately before execution, and again before comparison. A mutation of any of
the four branch-copy files after attestation blocks the V2 runner before invocation.

Atomic private no-replace writes are also used for attestations, branch outputs and the comparison
report. Interrupted temporary writes are removed without replacing an existing immutable target.

## Fork and strict separation

Each daily run has physically separate roots:

```text
run/
  legacy/{input,queues,corpus,database,logs,attestations}/
  v2/{input,queues,corpus,database,logs,attestations}/
  comparison/daily-comparison.json
```

Legacy and V2 input files are separate inodes copied from the same pinned verified byte set. Each
branch writes a canonical `0400` consumption attestation containing its branch, consumption time,
relative copy path and the exact snapshot id/manifest/checksum/payload hashes plus item count.

`BranchWorkspace` exposes writes only below that branch's `queues`, `corpus`, `database` and `logs`
capabilities. There is no publication capability. Portable relative-path canonicalization and
no-follow directory traversal reject attempts to reach sibling Legacy paths from V2, including
through `..`, absolute paths or a symlink planted below a V2 area. Before V2 execution, the completed
Legacy tree is recursively sealed and hashed; any subsequent byte, membership or mode drift fails
the V2 isolation result.

Fork setup and runner outcomes are independent. Legacy copy/execution happens first; a V2 copy or
runner failure remains a V2 result and does not replace, retry or reinterpret Legacy success/failure.

## Legacy baseline limits

Stage 4 does not change Legacy rules and does not adapt its domain output. The only allowed wrapper
differences are:

- the input snapshot pathname;
- private workspace/file permissions;
- branch-local attestation and log locations.

For a successful baseline, exact output bytes and exit code must match. For a failure baseline, the
exception type and message must match exactly. A successful runner whose output or exit code differs,
or a success where the baseline requires failure, becomes a failed `LegacyBaselineMismatch`; the
actual structured result and output hash remain attached for diagnosis. The wrapper does not retry
or translate a genuine Legacy failure. Normal, output-drift, exit-drift and expected-failure
semantics all have synthetic regressions while the V2 branch still runs independently.

`BranchResult`, `MaterialDecision` and `LegacyBaseline` validate their runtime shapes rather than
trusting annotations: exact bytes, exit range, non-negative integer statistics, valid dates,
decision uniqueness, LLM/provider/model relationships, publication and health fields fail closed at
the boundary. Execution and comparison are also bound to the original full consumption attestation;
a canonical rewrite that keeps the snapshot identity but changes attestation time/hash is rejected.

## Daily comparison

`packages/domain/dual_run.py` emits canonical private JSON containing:

- exact source identity and independently reverified Legacy/V2 attestations;
- input item count;
- included, rejected and deferred ids/counts for each branch;
- only-Legacy and only-V2 sets;
- rubric, publication-date, duplicate and numeric-stat differences;
- LLM provider/model/fallback status;
- branch output hash, exit/failure, release/publication and health status;
- explicit `v2PublicationAllowed: false`.

A V2 runner failure is reported alongside the unaffected exact Legacy result. Comparison refuses to
trust cached execution metadata when an attestation or branch snapshot no longer verifies.

## Deterministic synthetic evidence

The disposable evidence run used `snap_20260819_synthetic01`, two fabricated `example.test`
materials and explicit second-precision timestamps:

- manifest SHA-256:
  `0cb1bb4fbc8e7f7185bda198207d8c433a44d10c883e62edc5d8a905759e14ff`;
- checksum-file SHA-256:
  `fffe0340a68336d22614a3e3f453369d2d386bae5e3891154b225760647875c4`;
- aggregate payload SHA-256:
  `f4e468e29acb5cd49bac3b3165be6f1b7fcf539ce8523260a1ff6c874dd7063e`;
- `candidates.jsonl` SHA-256:
  `63d191d4aff84ef54f579260c94bb8fe87c1cefb596460f96721264a1408515e`;
- `safe-evidence-index.json` SHA-256:
  `8b47217dc46d870cfe37b15f40cd905c83ea94bbba65f774db20b371e93e1a17`;
- Legacy attestation SHA-256:
  `3e30bc8f36d3b430b017735794b62297391b0ab7b28cef5eb5a9eeefc158e818`;
- V2 attestation SHA-256:
  `de8de083b4d791950cc4793c31b1aed96bc34c345d71fdbe00fa8b76b9c05d85`;
- comparison report: 2,594 bytes, SHA-256
  `16c186f7fe7c74aebd8398d61aee67597763faf11a8bd26a51eee99c402aff6e`;
- Legacy baseline match: `true`;
- V2 publication status: `not_published`.

The three identity hashes are pinned as golden regression values. Two independent snapshot renders
are byte-identical.

## Verification

Mandatory command from repository root:

```bash
./v2/scripts/verify.sh
```

Final result:

```text
[verify] locked Python 3.12 development sync
Resolved 16 packages; checked 14 packages

[verify] Ruff format check
33 files already formatted

[verify] Ruff lint
All checks passed!

[verify] strict mypy
Success: no issues found in 32 source files

[verify] pytest
64 passed in 1.81s

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
Files scanned: 44
Synthetic fixtures: 2
Runtime imports: Python stdlib/local only; browser module dependency-free

[verify] deterministic production artifact and manifest
Radar V2 production artifact: PASS
Runtime files: 24
Artifact SHA-256: d288c50e50740a16e08f841c2ca5c74ab204898faf2b35de50aebe6e069b59fa

Radar V2 verification: PASS
```

Focused Stage 4 suite:

```bash
cd v2
uv run --no-sync pytest tests/test_stage4_snapshot_fork.py -q
```

Result: **36 passed**. The full suite is **64 passed**.

Negative regressions cover every snapshot file, coordinated same-id rewrite, parent-path swap while
the directory is pinned, post-attestation mutation, canonical attestation rewrite, source mutation
between branch consumptions, extra member, owner-write/broad modes, hardlink, symlink store, symlink
escape, absolute/traversal path, immutable target collision, concurrent same-id race, malformed
runtime result/LLM shapes, V2 setup/runner failure, Legacy output/exit/failure baseline drift, exact
Legacy failure semantics and V2 publication rejection.

## Independent acceptance findings and remediation

The first adversarial pass deliberately changed only modes/semantics and found three acceptance
defects before commit:

- immutable `0400` files restored as owner-writable `0600` were accepted;
- Legacy output drift remained `status=success` with only an auxiliary false flag;
- a string statistic crossed the runtime boundary despite the integer annotation.

All three now fail closed and have dedicated regressions. The follow-up review also bound file reads
to the already opened snapshot directory and bound executions/comparisons to the original complete
attestation, with parent-swap and canonical-attestation-rewrite regressions. The full verification
above is after all remediations.

## Production non-regression

After the final Stage 4 gate, `radar-api.service` and `caddy.service` were both active with
`NRestarts=0`; public `/api/health` returned `ok`, the production healthcheck passed, and the Legacy
SQLite SHA-256 remained
`481d5d6c9b54a58f78f288fb29c0eb072d43e74d6c2db8b14044a3153cd8f7f7`.

## Working-tree scope

Accepted repository change scope:

```text
README.md
docs/migration-plan-review-2026-08-19.md
docs/radar-stage4-snapshot-fork-2026-08-19.md
v2/README.md
v2/fixtures/synthetic/stage4-collected-input.json
v2/packages/domain/__init__.py
v2/packages/domain/dual_run.py
v2/packages/domain/snapshot.py
v2/packages/storage/safe_files.py
v2/pyproject.toml
v2/tests/test_component_boundaries.py
v2/tests/test_stage4_snapshot_fork.py
v2/tools/check_isolation.py
```

Generated `v2/dist/` and tool caches remain ignored verification output.

## Residual risks and next boundary

- Stage 4 provides code-level path capabilities, no-follow writes, read-only sealing and mutation
  detection. Later deployment stages must additionally run Legacy and V2 under distinct OS service
  identities if hostile same-UID code is in scope.
- The creation identity must remain in the orchestrator's durable daily state and be supplied to the
  fork. The API requires it and fails closed; Stage 5 must persist/pass it without reconstructing it
  from the snapshot directory.
- Snapshot files intentionally exclude raw HTML, secrets and provider request/response payloads;
  only the safe evidence index enters the common snapshot.
- Stage 5 must build the accepted closed desired-state candidate from the verified V2 copy. It must
  not add publication, SQL/DDL, shared queues or a bypass around consumption verification.
