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

See `docs/radar-kx-production-fulltext-2026-08-21.md` for the accepted topology and operational
evidence, and `docs/radar-kx-issue-perimeter-2026-08-21.md` for the perimeter contract, the robots
override policy, and its production evidence.
