# ADR-0002: Radar V2 releases, publisher, public API and reporting

- Status: Accepted
- Date: 2026-08-19
- Depends on: ADR-0001
- Contract family: `radar-v2/1`

## Context

Radar V2 has three change rates and trust levels:

- application code/schema changes;
- daily/correction content changes;
- gazette HTML/assets changes.

Legacy mixes these concerns and lets an agent reason about success. V2 needs typed input, deterministic checks, explicit compatibility, recoverable cross-host activation and a final report that Project Manager cannot reinterpret.

## Decision

### 1. Separate release streams

- **Application release** contains API/web artifacts, migrations, runtime pin and compatibility manifest. It requires explicit owner approval and an application deploy lock.
- **Content release** contains a typed candidate-derived row delta and deterministic daily artifacts. It is automatic after all technical gates pass and uses a content publish lock.
- **Gazette release** contains validated HTML/assets plus typed gazette metadata mutations. It is initiated by an explicit user request and uses the content publish lock.

Application and content locks are mutually exclusive. Daily/correction/gazette packages cannot carry SQL, DDL or migrations.

### 2. Candidate contract

Project Manager emits one `candidate.schema.json` package manifest with:

- immutable candidate/idempotency identifiers;
- operation `daily|correction|gazette`;
- expected base release and logical DB state hash;
- snapshot manifest/payload hashes for daily;
- expected issue hash for correction;
- typed entity mutations only;
- relative, hashed asset descriptors;
- requested/attempted/effective LLM outcome;
- actor and reason.

Unknown properties are rejected. The package is data, never executable instructions.

### 3. Delta contract

Publisher derives `delta.schema.json`; Project Manager never authors it. Delta contains:

- exact base and target release IDs;
- before/after logical state hashes;
- schema/table-contract versions;
- deterministic ordered upserts/tombstones against an allowlist;
- expected row counts and per-table logical hashes;
- asset hashes and target compatibility.

Base mismatch, schema mismatch, sequence gap, undeclared DB drift or unknown table/column blocks activation. Active DB inodes are never mutated.

### 4. Publisher state and rollback

Publisher follows the state machine in `publisher-state-machine.yaml`. Its durable job journal is external to replicated SQLite.

Success requires both source and production to converge on the same final release marker and logical state hash. Any post-activation API/public smoke failure must restore the previous content pointer, reopen SQLite readers, verify the previous release/hash and report `rolled_back`. Failure to prove rollback yields `needs_reconciliation` and blocks future publishing.

Idempotency behavior:

- an already successful candidate returns the saved success result;
- an in-progress candidate is not duplicated;
- a recoverable failed candidate resumes only from an allowed checkpoint;
- an unknown/ambiguous state fails closed.

### 5. Compatibility

`compatibility-manifest.schema.json` is embedded in every application release. It pins:

- application release and git commit;
- exact SQLite runtime and required features;
- DB schema and table-contract versions;
- supported candidate/delta/result/gazette contract versions;
- public API version;
- artifact hashes.

A content candidate/delta must match the active application manifest exactly where declared exact. Schema migration is an application-release concern only.

### 6. LLM outcome

LLM outcome is orthogonal to publication success:

- `success`: requested model produced accepted output;
- `fallback`: another model or deterministic fallback produced accepted output;
- `unavailable`: no LLM output was accepted and deterministic non-LLM rendering is used.

The contract records requested model, every attempt, effective model/provider when one exists, and warnings. `fallback` or `unavailable` does not block structurally valid content. Project Manager must tell the owner explicitly.

### 7. Errors and exit codes

`error-taxonomy.yaml` and `publisher-state-machine.yaml` define stable error codes, retryability, severity, exit codes and operator action. Human text is explanatory only; automation branches on codes.

Core classes:

- validation/security rejection;
- base/schema/compatibility conflict;
- source/storage/artifact failure;
- transport/remote staging failure;
- activation/public smoke failure;
- rollback/reconciliation failure;
- lock/idempotency state;
- internal defect.

### 8. Project Manager report

Publisher returns `publisher-result.schema.json`. Project Manager transforms it into `project-manager-report.schema.json` but must preserve:

- candidate/release/operation/issue identity;
- publication and rollback status;
- source/production hashes;
- requested/attempted/effective LLM outcome;
- every warning requiring owner visibility;
- stable error code and next action.

Every invocation ends with one final user-visible report. Missing delivery is a workflow failure even if publication succeeded.

### 9. Public API boundary

`public-api.openapi.yaml` is published-only and read-only.

- Only GET operations are defined.
- No `/api/internal`, draft, queue, raw provider, local path or write endpoint exists.
- DTOs are explicit and do not mirror tables or use `SELECT *` semantics.
- Invalid input returns bounded JSON 4xx.
- Search/list limits are bounded.
- Health exposes release/schema/state identifiers, not filesystem paths.
- Draft leakage and absolute-path leakage tests are mandatory for every endpoint.

### 10. Gazette safety

Gazette assets use relative paths and hashes. HTML is a static artifact, not executable package metadata. Validation rejects path traversal, forms/write calls, unsafe URL schemes and unapproved scripts. Public activation uses the same rollback guarantees as content.

## Consequences

- Project Manager can remain conversational while publication semantics are deterministic.
- Daily publication can continue through total LLM failure without pretending primary-model success.
- Code/schema changes cannot enter automatic content flow.
- Public API and storage may evolve independently behind explicit DTO/version contracts.
- More schemas and validation tooling are required before implementation.

## Rejected alternatives

- One combined application/content release: rejected because daily automation must not migrate schema or deploy code.
- Production pull/poll model: rejected; synchronous source-controlled publisher was selected.
- Free-form candidate JSON: rejected because it cannot prove mutation coverage or prevent executable payloads.
- Public table-shaped API: rejected because production contains drafts and internal editorial state.
- Agent-authored success message without machine result: rejected because it can hide fallback, rollback or partial failure.
