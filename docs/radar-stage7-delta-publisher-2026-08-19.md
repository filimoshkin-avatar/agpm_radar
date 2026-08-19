# Radar V2 Stage 7: delta engine and local publisher simulation

Date: 2026-08-19

Status: accepted after the final gate recorded below.

## Scope and source of truth

Stage 7 implements only the full-seed, delta, transactional staging and disposable local
publisher boundary in `docs/migration-plan-review-2026-08-19.md` § Stage 7. The frozen inputs are:

- `contracts/v1/delta.schema.json`;
- `contracts/v1/publisher-result.schema.json` and `llm-outcome.schema.json`;
- `contracts/v1/publisher-state-machine.yaml`;
- `contracts/v1/sqlite-contract.yaml`;
- ADR-0001 and ADR-0002.

No Legacy runtime, production database, cron, service, Caddy, DNS or Local Ru configuration is in
scope. Radar has no GRACE `M-*`/`V-M-*` module map, so the affected files and contract sources are
recorded explicitly and the commit carries `GRACE-Delta: skip` with that reason.

## Implemented boundary

### Exact full seed

`v2/packages/delta/engine.py` exports an exact verified SQLite file plus a canonical manifest that
binds application release, content release, sequence, schema/table contract, physical SHA-256,
logical state and counts/hashes for all 23 replicated tables. Import is create-only: it validates
the manifest and seed, creates a new private inode, reopens it and repeats release, integrity,
foreign-key, FTS and all-table checks. A full seed therefore restores undeclared drift instead of
merging with it.

All source-database operations open one private, single-link file without following any path
component. Exact-byte reads and immutable SQLite queries remain bound to that descriptor through
`/proc/self/fd`; device/inode/mode/link-count/size/mtime/ctime are checked before and after. A
parent-path swap cannot make the logical proof refer to different bytes from the copied seed/base.

### Typed delta and transactional apply

The delta generator compares two verified release databases and emits deterministic ordered typed
operations only for contract-authorized content tables:

- full-row insert/upsert operations with canonical row after-hashes;
- typed tombstones for authorized deletes;
- optimistic `expectedBefore` row hashes or `absent` fences;
- one final publisher-owned `content_releases` insert;
- base/target release, sequence, schema and logical-state fences;
- before/after row counts and logical hashes for every replicated table, including snapshots,
  drafts and editorial queues.

Application-owned schema/compatibility/rubric rows cannot be changed by a content delta. Arbitrary
SQL/DDL, unknown tables/actions/fields, malformed values, unsafe asset paths, duplicate row
operations, missing/out-of-order releases and stale preconditions fail closed.

Apply first copies the pinned base bytes to a newly reserved `0600` staging inode. All operations
run in one SQLite transaction; failures roll back the transaction and never change the supplied
base. The engine then verifies integrity, foreign keys, FTS parity, release marker, logical state
and all 23 table counts/hashes, removes no evidence, checks for forbidden sidecars and reopens the
sealed staging path. Applying the same delta to its already-complete target is a verified no-op.

### Durable local publisher simulation

`v2/packages/publisher/state_machine.py` implements the accepted transition graph over the existing
hash-chained external audit journal. Invalid transitions fail; `NEEDS_RECONCILIATION` blocks new
candidates while preserving status/replay access for the candidate that set the latch. Every event
for one candidate must retain the same release/before/after identity.

`v2/packages/publisher/local_simulation.py` runs the graph against distinct private source and
disposable-production roots:

1. acquire the exclusive `radar_mutation` lock;
2. persist the exact canonical delta/LLM/issue-date input and reject candidate collisions;
3. stage and prove source and production copies;
4. retain immutable release files and the exact previous pointer;
5. atomically replace only the small `active.json` marker;
6. reopen and prove the expected release/state;
7. commit the source pointer only after the disposable public check;
8. persist a contract-valid owner result.

A post-activation failure restores and reopens the exact previous production pointer. If that
release/hash cannot be proven, the result is `needs_reconciliation` and future candidates are
blocked. Retries recover checkpoints before activation, after activation, between result save and
`SUCCEEDED`, and between a terminal rollback transition and result persistence. Successful retries
do not reapply the delta, and completed candidates return `already_succeeded`.

## Synthetic and adversarial acceptance

Focused command:

```bash
cd v2
uv run --no-sync pytest tests/test_stage7_delta_publisher.py -q
```

Final focused result: **15 passed**.

The suite covers:

- exact full-seed round-trip and deterministic reseed after drift;
- official delta and publisher-result JSON Schemas resolved only from local contract files;
- exact initial/terminal/blocking states and transition parity with the frozen state-machine YAML;
- daily and correction deltas, inserts/upserts/tombstones and all-table evidence;
- transactional apply, duplicate apply and optimistic conflict rollback;
- invalid sequence/table/action, SQL/DDL-shaped data and unsafe filesystem inputs;
- symlink, hardlink, broad-mode and parent-directory path-swap attacks;
- exclusive publisher lock and invalid state transition;
- normal activation, completed replay and fresh-process imports;
- exact same-candidate input replay and delta/LLM collision rejection;
- crash before activation, after activation, after result save and after rollback transition;
- proven rollback, unproven rollback latch and same-candidate reconciliation replay;
- explicit no-LLM warning/report semantics.

Acceptance review found and remediated four material boundary defects before commit:

1. storage validation errors could escape the public delta exception type;
2. byte copying and SQLite inspection initially reopened the source path separately, leaving a
   path-swap window;
3. result persistence and terminal state transitions had asymmetric crash windows, and the global
   reconciliation latch ran before existing-candidate lookup.
4. existing candidates did not bind every journal event and retry to the same release/hash and
   exact delta/LLM/issue-date input.

Each remediation has a dedicated regression in the final focused suite.

## Historical full-seed and delta acceptance

The retained real Stage 3 import was used read-only:

```text
/tmp/radar-stage6-historical-MONHtA/radar-v2-import.sqlite
```

All Stage 7 outputs are retained separately at:

```text
/tmp/radar-stage7-historical-W8f7Vk
```

Recorded evidence:

```text
base release: rel_e404ff802c3e3c71083529ed, sequence 0
base logical state: ef5b4c3ef7ddfcda05c5aad331043bcc576ec641683e05d74ce1162e1e7c7f41
full-seed bytes: 4,898,816
full-seed SHA-256: e285e439df3ebaef777b35e7e26b1a49c89a99f5ce8a0db7988310a6af906f1c
replicated rows before: 3,536
delta operations: 2 (one disposable source_rule plus the release marker)
target release: rel_content_stage7_historical, sequence 1
target logical state: 2c2c70370614e96d4461c47a1bf22f54eb92e77e30c13289c66183360320c4cb
replicated rows after: 3,538
```

The restored full seed exactly matched the source digest. The delta-applied database exactly
matched the independently finalized target by state hash, every table count and every table hash.
The retained Stage 3 input bytes remained unchanged. No live Legacy database was opened for write.

## Final gate

Mandatory command:

```bash
./v2/scripts/verify.sh
```

Final result:

```text
Ruff format: 51 files already formatted
Ruff lint: PASS
strict mypy: 51 source files PASS
pytest: 101 passed
contract validator: PASS (6 schemas, 8 examples, 23 tables, 11 API paths)
JavaScript syntax: PASS (1 module)
secret/Legacy isolation: PASS (63 files, 3 synthetic fixtures)
production artifact: PASS (39 runtime files)
artifact SHA-256: e2acbce9999b96b21f4176e3d71732a67905252ef91d45f259a760db260f432d
Radar V2 verification: PASS
```

`git diff --check` also passed.

## Production non-regression and next boundary

After the final gate, the Legacy SQLite SHA-256 remained exactly
`481d5d6c9b54a58f78f288fb29c0eb072d43e74d6c2db8b14044a3153cd8f7f7`.
`radar-api.service` and `caddy.service` were active with `NRestarts=0`; local `/api/health`
returned `ok`, and the production healthcheck passed for issue `2026-08-19` with three materials.
Stage 7 performed no service reload and no external mutation.

The next sequential boundary is Stage 8: the read-only API and frontend V2 over frozen public
views/DTOs, including bounded query validation, safe release markers, connection reopen semantics,
no-LLM/empty issue UI, gazette routes and visual/security tests. Stage 8 remains local and
disposable; Local Ru preparation does not begin before Stage 10.
