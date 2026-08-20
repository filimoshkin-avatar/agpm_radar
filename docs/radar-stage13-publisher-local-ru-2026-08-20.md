# Radar V2 Stage 13 — publisher integration with Local Ru

Date: 2026-08-20

Status: complete.

## Scope and ownership

Stage 13 installed the technical publisher-to-Local-Ru boundary only. Project Manager candidate
integration and cron remain Stage 14/15. Legacy production, its cron and its three pre-existing
working-tree changes were not modified.

## Restricted transport

- dedicated Ed25519 publisher key;
- `radar-v2-deploy` has a private home and no unrestricted key;
- authorized key uses `restrict` plus one forced command;
- sudoers permits exactly `/usr/local/libexec/radar-v2-remote-activator`;
- activator is versioned under `/opt/radar-v2-activator/releases`, with exact Python 3.12.3 /
  SQLite 3.45.1 environment;
- request schema is closed, bounded to 16 MiB and allows only `status`, `publish`, `rollback`;
- incoming and audit children are root-owned `0700`; all requests are retained in quarantine.

Configuration backup: `/root/radar-stage13-config-backup-20260820T0930Z` on Local Ru.

## Successful private delta

The accepted test delta changed only replicated private editorial state:

- inserted `queue_stage13_transport_probe_01` in `editorial_queue`;
- inserted the final `content_releases` marker;
- public issues/materials/gazette content was unchanged.

Final release: `rel_stage13_private_transport_01`.

Final logical state:
`2c6e1ba75b252a8de5a2e0a0413bd31d6aa50968ebee228522a61cd9da30bff6`.

Source and Local Ru have identical counts and logical hashes for all 23 replicated tables. Their
physical SQLite hashes differ, as expected for independently applied SQLite transactions:

- source: `e588beee0bbfe9fdf6a29052aa70ead306ddf21e2b2ea65272eab27903c7e4fe`;
- Local Ru: `86711019b89f5fea97eccbb10a49ed5c50e8640a47ebbdc2d054b17223a395fd`.

The Local Ru pointer is exact `radar-v2-api:radar-v2-api 0600`, link count 1, SHA-256
`cddae12192a7a6622420d0a03df5177235fce6c760025ae3dccbd86d4353a418`; file and directory fsync
are mandatory in the activation implementation.

## Failure and rollback rehearsal

All Stage 13 branches were exercised:

- malformed transfer rejected before staging;
- stale/base-mismatch delta rejected before mutation;
- injected loopback verification failure restored and proved the previous release;
- injected public verification failure restored and proved the previous release;
- simulated source-commit failure used the explicit restricted rollback action and restored the
  previous release;
- deliberately unavailable rollback verification created `NEEDS_RECONCILIATION` and blocked a
  subsequent forced-command request.

After independent pointer/DB/loopback/public proof, both blocking markers were retained as
`NEEDS_RECONCILIATION.resolved-*`; none was deleted. Final publishing is unblocked and source /
production converge.

During acceptance, three fail-closed implementation defects were found before final success:

1. private child mode had to be exact `0700`;
2. standalone activator required the same `PYTHONHOME`/`LD_LIBRARY_PATH` as the accepted API unit;
3. health state is named `databaseStateHash`, not `stateHash`.

The third defect initially produced a false reconciliation marker after a rollback that had in
fact restored the exact previous pointer. Both health endpoints and the immutable DB proved there
was no surviving public impact before the marker was resolved and retained.

## Verification

- Ruff format/lint: PASS (75 files);
- strict mypy: PASS (75 source files);
- pytest: PASS (150 tests);
- JavaScript and Legacy-parity console smoke: PASS;
- isolation/secret scan: PASS (99 files, three fixtures);
- deterministic public artifact: 26 files, SHA-256
  `9cb0f5e96cbebc0513737a13f7ced04fab69644a31d75964b2c24e68594306bb`;
- `git diff --check`: PASS.

Final Local Ru state:

- Radar API PID `37933`, `NRestarts=0`, loopback-only `127.0.0.1:8765`;
- Caddy PID `1021`, `NRestarts=0`;
- public `radar.agpm.space` health matches the final release/state;
- all seven NRD units active and NRD public health green;
- Radar API error journal empty for the Stage 13 window.

Legacy remains green on `radar.aipractice.space`, latest `2026-08-20` with 10 materials. No Legacy
DB, service, frontend, cron or Project Manager working-tree change was modified.

## GRACE delta

GRACE-Delta: skip — Radar has no GRACE module metadata. This report records affected transport,
publisher, deployment and test files, the source of truth, invariants, regression coverage and
remaining stage boundary.

Next stage: Stage 14 Project Manager end-to-end manual dry runs without cron activation.
