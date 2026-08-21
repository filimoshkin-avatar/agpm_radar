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

See `docs/radar-kx-production-fulltext-2026-08-21.md` for the accepted topology and operational
evidence.
