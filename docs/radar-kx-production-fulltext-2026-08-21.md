# Radar KX production full-text contour

Date: 2026-08-21

Status: complete and accepted in production.

## START_CHANGE_SUMMARY

- Goal: create an additional production PostgreSQL evidence store on Local Ru and preserve the
  complete historical Radar corpus with versioned full text.
- Governed source of truth: this document, `kx/sql/001_initial.sql`, the immutable raw/version
  contracts, and the deployment templates under `kx/deploy/`.
- Affected files: the new isolated `kx/` package and this report only.
- Existing Radar V2 application/content releases, API, Caddy, publisher and dual-run contracts are
  explicitly out of scope and must remain byte/configuration unchanged.
- GRACE module delta: skip — Radar has no `M-*`/`V-M-*` module map. Affected files, invariants,
  regression tests, production evidence and operational risks are recorded explicitly instead.

## Production invariants

1. `radar_kx` is a separate loopback-only PostgreSQL database and role on Local Ru.
2. Decoded HTTP entity bodies are immutable gzip blobs addressed by SHA-256 inside PostgreSQL;
   status, final URL and selected response headers retain the HTTP provenance.
3. Canonical text is complete, untruncated, versioned, hashed and immutable.
4. A changed source creates a new version; a repeated body reuses the existing raw blob/version.
5. Every historical material remains linked to its canonical document, including failed fetches.
6. The queue is resumable after crash/reboot and records terminal, retry and refusal states.
7. The fetcher rejects private/non-global targets, validates redirects, limits body size and rate
   per host, respects robots by default, and never asks LangExtract to fetch URLs.
8. Ingest stops before Local Ru free space falls below 20 GiB.
9. PostgreSQL backups are self-contained and retained without automatic deletion.
10. Radar V2 health, release pointers, service PID/restart count and public behavior remain intact.

## Acceptance evidence

## Installed production boundary

Radar KX is deliberately adjacent to, not embedded in, the public Radar V2 runtime:

- PostgreSQL 17 database and login role: `radar_kx`; connection limit 12; local peer access only;
- extensions: `pgcrypto`, `pg_trgm`, `unaccent`, and `vector` 0.8.6;
- application releases: `/opt/radar-kx/releases/<release-id>` with a relative, atomically switched
  `/opt/radar-kx/current` pointer;
- isolated copied Python 3.12.3 base and read-only dependency runtime under
  `/opt/radar-kx/runtime/`;
- immutable imported inputs under `/var/lib/radar-kx/imports/20260821-historical`;
- self-contained retained dumps under `/var/backups/radar-kx`;
- resumable ingest and daily backup timers named `radar-kx-ingest.timer` and
  `radar-kx-backup.timer`.

PostgreSQL remains bound to loopback. No KX port, Caddy route, DNS record, public API, firewall rule,
Legacy object or Radar V2 application/content release was added or changed.

## Historical corpus imported

Both retained corpus revisions were imported, old first and current second:

- 7,653 material rows, SHA-256
  `eb4c51b11e85840d17267e48c4810afeb6eb97cb018ab24525a19648fba18a5d`, producing
  7,651 normalized documents;
- 8,310 material rows, SHA-256
  `b17bc175f9aca0e87f788b4ffc41487671e145aee12bbc0f37862c2b5aaa5844`, producing
  8,308 normalized documents;
- all 15,963 material/corpus revisions are append-only in `source_material_revisions`; the latest
  8,310 rows are queryable through `source_materials`;
- the current corpus is a strict material-ID superset of the old corpus: 657 added, none removed;
- 246 retained source snapshots were replayed; 234 yielded canonical text and 12 retained raw
  evidence without acceptable text;
- 69 current and 49 old non-empty 20,000-character legacy caches were imported as explicitly
  incomplete versions, never represented as full articles;
- three retained snapshot URLs are no longer present in the current manifest, so the evidence
  store correctly contains 8,311 documents rather than only the 8,308 current-manifest documents.

The raw import archive contains 639 files / 136,623,526 bytes and has SHA-256
`2246bbed3ce90f778e14f51d5b2585cbe1ec25aae17109cdc870a02607b68352`. Every extracted input
was checked against its per-file SHA-256 manifest before database import.

## Storage and provenance contract

- `corpus_imports`, `source_materials`, and append-only `source_material_revisions` preserve the
  original JSON payload and its deterministic hash.
- `raw_blobs` stores the exact decoded HTTP entity body seen by the parser, gzip-compressed with a
  deterministic mtime and addressed by the SHA-256 of the uncompressed bytes.
- `fetch_attempts` records source kind, requested/final URL, timing, status, selected HTTP headers,
  body hash, outcome, error and worker release. It is immutable.
- `document_versions` stores untruncated canonical text, text hash, parser/config version, quality,
  completeness and fetch time. It is immutable.
- Canonical text is split into lossless, non-overlapping chunks of at most 4,000 characters. The
  concatenated chunks reproduce the exact canonical text and carry Russian and English PostgreSQL
  FTS indexes.
- A database trigger rejects updates/deletes to raw blobs, attempts, versions and material
  revisions. Another trigger accepts `match_status=exact` evidence only when claim/version match,
  quote SHA-256 matches and the 0-based character range reproduces the quote exactly.
- Future embeddings, entities, claims, metrics, relations, ideas and versioned idea scores have
  dedicated tables; they are derived state and do not weaken the source/evidence boundary.

## Fetch and backfill behavior

- only public global HTTP(S) targets are accepted; credentials in URLs, private DNS answers and
  unsafe redirects are rejected;
- the systemd unit adds a second SSRF barrier: loopback, private, carrier-grade NAT, link-local,
  documentation, multicast and reserved destinations are denied by the cgroup network policy;
  only `127.0.0.53` is allowed inside loopback for the host's systemd-resolved DNS stub;
- environment proxy variables are ignored by the HTTP client;
- robots policy is enabled by default; a real robots 404 allows fetching, permanent protected
  responses fail closed and transient robots failures remain retryable;
- body size is capped at 15 MiB and free disk may not cross the 20 GiB reserve;
- one global limiter permits at most one request per second per host;
- 32 bounded worker slots use rolling concurrency, with at most eight active leases per host, so a
  large rate-limited source cannot occupy the entire executor;
- robots policies are cached once per origin; different origins load concurrently, while threads
  for the same origin share one origin-specific lock;
- Reddit post URLs use the JSON representation when permitted by robots;
- Telegram post URLs use the public embed representation, because the ordinary page contains no
  message body; source-specific message text is kept without reactions/view-count UI, and a clean
  post of at least 20 characters is complete even when shorter than the generic article threshold;
  the original requested URL remains provenance;
- Pandaily's public Remix response is decoded through its bounded devalue graph and only the
  current `routes/$slug -> post -> content` body is accepted; larger featured-post payloads are not
  mistaken for the requested article;
- corrupt PDFs and arbitrary parser exceptions retain the fetched body and fail only that document;
- canonical parser v3 replaces every PostgreSQL-forbidden NUL with one Unicode replacement
  character, preserving length and offsets while retaining the unchanged raw body.

The network-policy smoke proved public DNS/TCP still succeeds. Against the same active Radar V2
loopback endpoint, an unrestricted `radar_kx` transient unit returned `connect_ex=0`, while the
restricted policy returned a nonzero kernel result and could not connect.

Every manifest document has a queue row even when robots, deletion, access control, paywall,
timeout or parser quality prevents a complete version. Such rows stay explicitly failed/retryable;
metadata or a 20,000-character legacy excerpt is never relabelled as full text.

## Retained validation and recovery evidence

- pre-change evidence: `/root/radar-kx-prechange-20260821T151309Z` on Local Ru;
- deployment evidence and all superseded/failed attempts:
  `/root/radar-kx-deploy-20260821T1522Z`;
- retained schema validation database: `radar_kx_validation_20260821`, 24 KX tables;
- schema negative tests proved that an invalid evidence span and a raw-blob update are rejected;
- the accepted runtime is an exact copied Python 3.12.3 base plus a read-only uv-installed runtime;
  the failed ensurepip runtime is retained and is not a current target;
- the first failed backup directory is retained with its failure marker;
- the first valid online backup is `/var/backups/radar-kx/20260821T155733Z` (79 MiB custom dump,
  SHA-256 checked, TOC readable);
- retained restore database: `radar_kx_restorecheck_20260821t155733z`;
- the restored snapshot passed full decompression, byte-size, raw SHA-256, canonical-text SHA-256,
  version-ID and best-version checks with `errorCount=0`.

The original restore verification intentionally remains beside the corrected verification. It
exposed that the first verifier used the current parser hash for old parser-v1/v2 versions; the
corrected verifier uses each row's stored `parser_config_sha256`.

## Final acceptance evidence

The accepted immutable application release is
`/opt/radar-kx/releases/radar_kx_release_20260821_54461cf50a73`; its manifest SHA-256 is
`54461cf50a73ac0ce5b1a121fed016ce11ea375f8f849c8b19ee54b72fdb123d` and every listed file
passes the manifest check. The transferred release archive SHA-256 is
`5579f0fa08fdd5a4ea01370e71f68fbea41038da8b1f922c6c977794acd87b2a`. The active dependency
runtime is
`/opt/radar-kx/runtime/releases/radar_kx_runtime_20260821_51284b732007_uv1`.

After the full initial pass and all controlled retries, production contains:

- 8,310 material records and 15,963 immutable material revisions;
- 8,311 canonical documents, including the three retained snapshot-only URLs;
- 5,904 successful and 2,404 terminal, explicitly classified network queue rows, with no pending,
  retry or running rows;
- 6,759 document versions, of which 6,457 are complete; 5,951 documents have a complete best
  version;
- 6,654 raw blobs containing 946,133,712 uncompressed bytes and 236,057,482 stored compressed
  bytes;
- a 714,946,227-byte production database at final verification time.

All historical material records and their original payloads are therefore preserved. Full public
article bodies are present wherever the retained evidence or a policy-compliant network fetch
could obtain them. This is deliberately not reported as 8,311 complete bodies: 2,404 network rows
remain unavailable after retries. Their primary terminal causes are 1,886 robots denials, 299 HTTP
403 responses, 91 weak/missing bodies, 41 timeouts, 40 HTTP 429 responses and 28 HTTP 404
responses; the remaining 19 rows are classified network, size, parse, authentication, server or
redirect failures. Reddit accounts for 1,745 robots denials. Radar KX did not bypass robots,
authentication, paywalls or source access controls and did not fabricate full text from metadata.

The complete-version language distribution is 4,303 Russian, 1,810 English, 316 mixed and 28
undetermined versions. Actual PostgreSQL FTS smoke queries found 993 chunks for
`искусственный интеллект` and 633 chunks for `artificial intelligence`.

The production full verifier completed with `status=ok`, `errorCount=0`. It decompressed and
rehash-checked every raw blob, rechecked sizes, canonical-text hashes, stored parser/config IDs,
document IDs, chunk continuity, exact offsets, chunk hashes and complete-version coverage, as well
as material revision payload hashes and corpus counts.

The final retained backup is `/var/backups/radar-kx/20260821T171813Z/radar_kx.dump`, a
318,205,602-byte PostgreSQL custom dump with SHA-256
`8ad17c4e4f21557d81e333c8ebbfd9151d549fd16d584ae5ec2e0cd262cdcc4f`. Its checksum and
`pg_restore` TOC passed. A clean restore into
`radar_kx_restorecheck_20260821t171813z_v2` reproduces every logical count above and independently
passes the same full verifier with `errorCount=0`. The first final restore attempt exposed an
extension-ownership ordering issue; that failed database and its logs are retained unchanged, and
the accepted restore uses archived object ownership while PostgreSQL owns the extensions.

Both production timers are enabled and active. The resumable ingest and backup oneshot services
are inactive after successful exit with status 0 and zero restarts. `systemd-analyze security`
reports exposure 2.8 (`OK`) for ingest and 3.0 (`OK`) for backup. PostgreSQL remains loopback-only,
the `radar_kx` role is non-superuser/non-createdb/non-createrole/non-replication/non-bypass-RLS with
a 12-connection limit, and no new public listener exists. After the production database, retained
restore checks and final dump, the Local Ru root filesystem still has approximately 52 GiB free;
ingest additionally enforces a 20 GiB hard reserve.

Radar V2 remained unchanged throughout acceptance:

- active application release:
  `/opt/radar-v2-api/releases/app_release_20260821_530a3c5`;
- content release `rel_1c420e848b99357be9b53106` and database state
  `617ab2c54f1fc5387adeec2c77c132ec18d579ce2f7991cb25347bd40fce21ca`;
- loopback and public health bodies are byte-identical with SHA-256
  `041f654dbcca2bfd5246474f04ba072089669f0bf936e22add5b2531c3cd6990`;
- the Radar V2 API PID remains 70308, PostgreSQL PID 1124 and Caddy PID 1025; all three have zero
  restarts;
- application target membership, application file hashes, Caddy/Radar configuration and the
  pre-existing PostgreSQL configuration all match the pre-change evidence.

Local repository gates pass: 32 regression tests, Ruff formatting, Ruff lint/import order, strict
mypy, the locked-requirements export comparison and `git diff --cached --check`.

Final evidence is retained at `/root/radar-kx-final-acceptance-20260821T1714Z` on Local Ru. Failed
and superseded releases, backup attempts, restore attempts and validation databases were retained;
nothing was deleted. This completes the productive storage and collection contour. Knowledge
extraction, embeddings, entity resolution, evidence-grounded answer APIs, graph views and idea
ranking remain separate derived stages over this accepted source-of-truth database.
