# Radar V2 Stage 1: contracts and ADR evidence

Date: 2026-08-19

Status: completed

## Accepted decisions

- ADR-0001 fixes one replicated SQLite graph, stable issue IDs, draft/published lifecycle, explicit writer roles, single mutation lock, historical inference, logical hashing, sealed DB rules and derived published-only FTS/views.
- ADR-0002 fixes separate application/content/gazette streams, candidate/delta boundaries, publisher rollback semantics, compatibility, LLM outcomes, error codes, Project Manager reporting and public API isolation.
- SQLite build profile is pinned to 3.45.1, source id `e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257ccalt1`, FTS5, THREADSAFE=1, `application_id=RAD2`, `user_version=1`.
- Project Manager candidate is a closed domain desired-state package, not table mutations. Daily, correction and gazette are separate `oneOf` branches.
- Publisher-generated delta uses generated table/action-specific schemas from the SQLite contract: exact PK, closed full row, expected-before fence and explicit tombstone.
- Remote activation order is pointer activation, API reopen, loopback verification, public verification, then source commit.
- LLM outcome supports primary success, model fallback, unavailable with deterministic implementation, and not-requested.
- Public API is GET-only, published-only, explicit DTO, bounded input and safe URL scheme.

## Contract artifacts

Machine contracts are in `contracts/v1/`:

- candidate, delta, publisher result, Project Manager report, compatibility and reusable LLM JSON schemas;
- SQLite, historical inference, publisher state machine and error taxonomy YAML;
- OpenAPI 3.1 public contract;
- eight valid examples.

Tools:

- `tools/contracts/generate_delta_schema.py` generates the closed delta schema from the authoritative table contract;
- `tools/contracts/validate_contracts.py` validates schemas, examples, cross-contract table/PK/action rules, state reachability, error/exit-code mapping, OpenAPI local references/public boundary, forbidden host paths/executable keys and negative contradiction/injection cases.

## Frozen historical evidence

`fixtures/legacy-baseline/all-issues-evidence.json` freezes all 74 baseline issue dates with hashes for DB rows, canonical reports, raw DOCX, normalized JSON and public JSON. All material/stat invariants pass. Legacy `status` remains provenance only.

## Verification

```text
Radar V2 contracts validation: PASS
JSON schemas: 6
Examples: 8
SQLite tables: 23
Public API paths: 11
```

Negative gates prove rejection of:

- executable/unknown candidate fields;
- unsafe material URL scheme;
- unknown delta columns;
- contradictory published publisher result;
- contradictory published Project Manager report.

No absolute production paths or secrets are present in domain examples/contracts.

## Plan impact

Stage 2 must install contract generation/validation as mandatory local/CI gates and pin the same SQLite build profile. Stage 3 must import through the frozen 74-issue evidence manifest and immutable provenance tables. Stage 5 implements candidate creation against the closed desired-state schema. Stage 7 uses only generated typed deltas and requires every replicated table in before/after expectations.

No runtime, cron, Legacy DB, Caddy, Local Ru or DNS state changed in Stage 1.
