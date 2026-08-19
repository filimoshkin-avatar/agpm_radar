# Radar V2 Stage 5 — Project Manager playbooks

Status: accepted Stage 5 operator interface. These commands construct or verify candidates only.
They do not publish, activate content, call Local Ru, change cron, reload services or write Legacy.

## Common preconditions

- Work from the repository `v2/` directory in the locked Python 3.12 environment.
- Candidate JSON must validate against `contracts/v1/candidate.schema.json` and the stricter runtime
  semantic checks.
- Source SQLite must be a private single-link regular file below a real, non-symlink directory.
- Staging and package-store parents must already exist and be private; staging must not exist.
- A package store may contain only the private builder lock plus valid immutable candidate packages.
- No command accepts SQL, DDL, migrations, shell commands, credentials or production host paths.

Development invocation prefix:

```bash
cd /mnt/vdd/Radar/v2
uv run --no-sync python -m apps.candidate_builder
```

The same module is included in the deterministic production artifact. Stage 5 does not install or
run that artifact on a production host.

## Daily candidate

Daily additionally requires the Stage 4 `v2` branch workspace. Its immutable consumption
attestation must bind the manifest to the exact snapshot id, manifest hash, payload hash, checksum
hash and item count.

```bash
uv run --no-sync python -m apps.candidate_builder daily \
  --candidate candidate-daily.json \
  --source-db state/source.sqlite \
  --staging-db state/staging/daily.sqlite \
  --package-store state/candidates \
  --v2-workspace state/runs/run-id/v2
```

The builder checks the expected base release/sequence/logical hash, requires the issue id/date to be
absent, derives snapshot/issue/material/analysis/quality/rubric/stats/LLM/queue/draft mutations, and
replays them into the new staging database.

## Historical correction

```bash
uv run --no-sync python -m apps.candidate_builder correction \
  --candidate candidate-correction.json \
  --source-db state/source.sqlite \
  --staging-db state/staging/correction.sqlite \
  --package-store state/candidates
```

The target date must resolve to the declared issue. The current issue aggregate must match
`expectedIssueStateHash`; every shared-material precondition must match its complete current row.
Correction can replace the desired issue aggregate but cannot modify immutable Legacy provenance.

## Gazette candidate

The asset tree must be private and contain only regular single-link files under the exact relative
paths declared by `inputAssets`. Text assets must be UTF-8. Membership, bytes, media types, SHA-256,
entrypoint, secret/path/executable scrubbing and gazette optimistic state are checked before SQLite
replay.

```bash
uv run --no-sync python -m apps.candidate_builder gazette \
  --candidate candidate-gazette.json \
  --asset-root state/gazette-assets \
  --source-db state/source.sqlite \
  --staging-db state/staging/gazette.sqlite \
  --package-store state/candidates
```

## Status and retry

Both commands reopen the complete immutable package, require exact directory/file modes
`0500`/`0400`, single-link files, exact membership/checksums/canonical JSON, candidate/mutation
bindings and preview parity.

```bash
uv run --no-sync python -m apps.candidate_builder status \
  --package state/candidates/candidate-id

uv run --no-sync python -m apps.candidate_builder retry \
  --package state/candidates/candidate-id
```

`retry` returns `ready_for_publisher_retry`; it does not call a publisher. Publication begins only
in later stages after publisher/delta/renderer/remote controls exist.

## Final Project Manager report

```bash
uv run --no-sync python -m apps.candidate_builder report \
  --publisher-result publisher-result.json \
  --output project-manager-report.json
```

The adapter rejects contradictory status/exit/publication/rollback data and malformed checks,
warnings, timestamps, hashes or ids. It preserves the full requested/attempted/effective LLM
outcome. Fallback and complete LLM outage remain explicit in both machine fields and owner-visible
warnings. `--output` is create-only and never overwrites an existing file.

## Machine result and failure semantics

Success is one canonical JSON object on stdout with candidate id, operation, LLM status, immutable
package hash and staging replay counts/hash. Failure is canonical JSON on stderr with exit code 2.
Malformed or duplicate candidates, duplicate idempotency keys, source/base drift, unsafe paths,
asset mismatch, staging collision and tampering all fail closed. A failed validation never creates a
candidate package; an already registered package is never overwritten.
