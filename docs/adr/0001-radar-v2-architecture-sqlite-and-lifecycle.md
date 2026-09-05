# ADR-0001: Radar V2 architecture, SQLite and publication lifecycle

- Status: Accepted
- Date: 2026-08-19
- Decision owners: Ivan Filimoshkin, Radar V2 maintainers
- Contract family: `radar-v2/1`

## Context

Legacy Radar grew as an MVP. Its public SQLite contains 74 publicly served issues, but every issue is stored as `status='draft'` with `published_at=NULL`. The database is rebuilt from corpus/report artifacts and is not a trustworthy publication ledger. Project Manager, LLM calls, rendering, SQLite writes and public serving are coupled.

Radar V2 must preserve Project Manager as editorial owner while moving all validation, storage mutation, rendering and production activation into deterministic software. Source and Local Ru must use the same SQLite schema and contain the same replicated rows, including drafts. Public readers must see only published DTOs.

## Decision

### 1. Planes and authority

- **Project Manager** collects sources, performs editorial/LLM work and creates a typed candidate package. It never executes SQL or writes production paths.
- **Source publisher** is the only authoritative content writer. It applies typed mutations to a staging copy, seals it, creates a row-level delta, coordinates remote activation and atomically commits the source copy.
- **Application migrator** is the only schema writer. It runs only in an explicitly approved application release.
- **Bootstrap importer** may write source staging only before release 0 and is permanently disabled afterward.
- **Remote activator** is mirror-only: it applies a validated publisher delta to a production staging copy and atomically switches the content pointer after verification.
- **Public API** opens the active production SQLite through `mode=ro&immutable=1`, then sets `query_only=ON`; pointer activation always forces connection reopen/restart and release/hash verification.
- **Caddy** is the only public listener. It does not read editorial data directly.

Writer roles are disjoint and machine-readable in `contracts/v1/sqlite-contract.yaml`. Content, gazette and application mutations use one exclusive FD-backed `radar_mutation` lock per host with fixed source-then-remote acquisition order, fencing by base release/state/schema/sequence and a reconciliation latch after any ambiguous operation.

### 2. One replicated SQLite contract

Source and Local Ru contain the same replicated contract tables and rows. Host-local publisher journals, transport attempts, raw provider payloads, secrets and absolute filesystem paths are not stored in replicated SQLite.

The physical V1 schema consists of:

- schema/application metadata;
- immutable source snapshot metadata;
- materials and source/evidence metadata;
- editorial queue state;
- issues and issue-material relations;
- material/issue analyses and LLM outcome summaries;
- rubrics and daily statistics;
- gazette metadata/assets;
- final content release markers and application compatibility markers.

~~FTS5 table `published_materials_fts` is derived only from versioned view `pub_search_documents_v1` with tokenizer `unicode61 remove_diacritics 2`. No triggers or delta operations target FTS virtual/internal tables. They are excluded from canonical hashing, rebuilt in deterministic order after every apply, and checked through projection count, FTS integrity and golden-query parity.~~ **Superseded by ADR-0014 (2026-09-05): both objects were removed by migration 0004. Search runs in the application, over the texts a card shows.**

### 3. Draft and published lifecycle

`issues.lifecycle_status` is `draft` or `published`. Issues use an explicit stable `issue_id` primary key and unique `issue_date`; material publicity exists only through `issue_materials -> issues(lifecycle_status='published')`.

- New V2 publication requires `publication_origin='v2'`, non-null `published_at` and `content_hash`.
- A Legacy-imported public issue may have unknown original publication time. It is imported as `published` with `published_at=NULL`, `publication_origin='legacy_inferred'` and explicit provenance.
- A draft is physically replicated to Local Ru only as part of a validated publisher operation.
- Versioned `pub_*_v1` views must filter `lifecycle_status='published'` at the SQLite query boundary. API code may read only those views and `pub_health_v1`; implementation should additionally install an SQLite authorizer when supported. (The derived published FTS named here was removed by ADR-0014.)
- Material membership is represented by `issue_materials`; removing an item from one issue deletes/unpublishes that relation, not the global material row.
- Historical correction overwrites the current issue state. Product revisions are not exposed. Before/after snapshots, delta, hashes, actor and reason are retained in external technical audit/backup artifacts.

### 4. Historical-publication inference

Legacy `status` and `published_at` are provenance, never V2 lifecycle authority.

An issue is inferred as historically public only when all required evidence exists:

1. the issue exists in the Stage 0 baseline SQLite identified by its database SHA-256;
2. a canonical Legacy Markdown or DOCX report exists for that issue date;
3. the generated public JSON cache contains `issues/<date>.json` for that issue;
4. issue/material/stat invariants pass, including an explicit valid empty issue;
5. the issue date belongs to the frozen Stage 0 baseline range.

The importer records each evidence item and its hash in immutable `legacy_issue_provenance` and `legacy_publication_evidence` rows. The initial 74 dates are frozen in a per-artifact evidence manifest rather than inferred from a date range; `2026-06-07` has an explicit weekly-name mapping. Ambiguous rows are imported as draft/review-required, never silently published. The full inference contract is `contracts/v1/historical-publication-inference.yaml`.

### 5. SQLite runtime and sealed artifacts

Both source publisher and Local Ru activator use the same pinned SQLite build profile: version `3.45.1`, source id `2024-01-30 16:01:20 e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257ccalt1`, `ENABLE_FTS5` and `THREADSAFE=1`. SQLite identity is `application_id=1380009010` (`RAD2`) and `user_version=1`. A version/build change requires an application release and compatibility evidence.

Writers operate only on staging copies with `foreign_keys=ON`, `trusted_schema=OFF`, `journal_mode=DELETE`, `synchronous=FULL`, `secure_delete=ON`, `temp_store=MEMORY`, `busy_timeout=5000` and `BEGIN IMMEDIATE`. Replicated IDs/timestamps are explicit candidate data; `AUTOINCREMENT`, local `now()` and `random()` are forbidden. Before a DB becomes an immutable content artifact it must:

- checkpoint/truncate sidecar journals;
- use sealed `journal_mode=DELETE`;
- have no `-wal`/`-shm` sidecars;
- pass `integrity_check` and `foreign_key_check`;
- have expected schema/application IDs;
- expose an expected logical state hash and file SHA-256.

### 6. Hash semantics

Two hashes are distinct:

- **File SHA-256** proves transport integrity of a concrete SQLite artifact.
- **Logical domain state hash** proves source/production domain equality independent of page layout. It is SHA-256 over canonical UTF-8 JSON lines for every state-hashed replicated table, ordered by table name and primary key, with explicit JSON/null/number normalization. Self-referential/compatibility metadata tables (`schema_migrations`, `application_compatibility`, `content_releases`) are excluded from this aggregate and are compared through separate per-table logical hashes.

Delta base/target checks use the logical domain state hash plus metadata per-table hashes. Full seed transfer additionally verifies file SHA-256.

### 7. Paths, secrets and evidence

Replicated domain rows contain only stable IDs, public URLs, safe metadata and content. They do not contain:

- `/root`, `/mnt`, `/etc`, `/srv`, `/opt` or `/var` paths;
- OpenClaw session/profile identifiers unless explicitly modeled as non-secret actor IDs;
- OAuth/API keys or bearer tokens;
- raw HTML or full provider request/response bodies;
- arbitrary SQL, DDL or migration text.

Large/raw evidence is stored outside SQLite and referenced by content-addressed opaque IDs, never source-host paths.

## Consequences

### Positive

- Project Manager remains the editorial interface without holding production write authority.
- Source and Local Ru can be reconciled deterministically.
- Drafts are available for failover/operations but cannot leak through public DTOs.
- Historical Legacy data is preserved without treating the broken Legacy lifecycle as truth.
- Corrections stay simple for users while rollback remains technically possible.

### Costs

- A staging-copy publisher and logical hasher are mandatory.
- Cross-host activation needs explicit compensation/rollback states.
- Exact SQLite runtime parity must be maintained in application releases.

## Rejected alternatives

- Copying only published rows: rejected because full draft/published replication is required.
- Direct Project Manager SQL: rejected because it bypasses deterministic validation/audit.
- Treating Legacy `status='draft'` literally: rejected because all known public history would be misclassified.
- Raw SQLite file hash as state identity: rejected because logically equal databases can have different page layouts.
- Product-visible immutable issue revisions: rejected by owner; external technical rollback evidence remains mandatory.
