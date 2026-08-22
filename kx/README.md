# Radar KX

Radar KX is an isolated production evidence store for the historical Radar corpus. It does not
write to Legacy Radar or Radar V2 and is not imported by the dependency-free V2 public API.

The first production boundary provides:

- immutable, content-addressed decoded HTTP entity bodies inside PostgreSQL;
- immutable, versioned canonical full text without truncation;
- resumable per-document fetch queues with bounded retries and leases;
- per-host rate limiting, redirect validation, body-size limits, and private-network rejection;
- PostgreSQL Russian/English full-text indexes and future pgvector/knowledge tables;
- deterministic corpus/cache import, verification, status, and backup commands.

The second boundary adds the Radar issue perimeter: an immutable, audited snapshot of which
materials a published Radar issue actually selected, linked to the canonical KX documents those
selections point at. `scripts/export_v2_perimeter.py` reads the active Radar V2 content release
read-only and emits the artifact that `import-perimeter` ingests; `perimeter-status`,
`perimeter-gaps`, `perimeter-prepare`, and `perimeter-reparse` drive and audit the completion pass
for perimeter documents that still lack full text.

The third boundary is the corpus-membership contract: Radar counts its materials in five stores
with four different units, and `scripts/corpus_membership_kx_extract.sql` plus
`scripts/corpus_membership_report.py` reconcile all of them read-only, failing when any transition
between two layers stops being explained. `src/radar_kx/corpus_membership.py` holds the logic;
`docs/radar-kb-corpus-membership-contract-2026-08-22.md` is the agreed contract it enforces.

`scripts/wiki_inventory.py` reads the AgPM file wiki the Project Manager maintains and reports what
is in it: layers, which SCHEMA.md page conventions each page follows, atomic claim candidates with
line numbers, which pages cite a source, and the untyped link graph. It writes nothing back;
`docs/radar-kb-wiki-inventory-2026-08-22.md` records the measurement and what it changes.

The `003` boundary records how evidence was obtained and prepares publication: an append-only
`version_provenance` beside every version (a version id does not cover its own acquisition method,
so a wrong one can only be corrected beside it), a fail-closed `version_publication_block`, the
egress audit, wiki snapshots, store-reconciliation reports and the corpus membership class.
`radar_kx import-artifact` is the offline import path - files, never a fabricated HTTP 200 - and
`radar_kx record-provenance` appends provenance to versions that already exist.
`scripts/verify_migrations.sh` applies every migration to a throwaway database, on the repository
baseline and on the schema production drifted into, and runs the SQL-backed tests that
`scripts/verify.sh` skips. `docs/radar-kb-migration-003-runbook-2026-08-22.md` is the runbook.

See `docs/radar-kx-production-fulltext-2026-08-21.md` for the accepted topology and operational
evidence, and `docs/radar-kx-issue-perimeter-2026-08-21.md` for the perimeter contract, the robots
override policy, and its production evidence.
