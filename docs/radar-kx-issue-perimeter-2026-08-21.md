# Radar KX issue perimeter and product plan

Date: 2026-08-21

Status: production issue-perimeter layer deployed; remaining unavailable full texts are explicitly
classified.

## START_CHANGE_SUMMARY

- Goal: make the Radar V2 issue selections a first-class KX perimeter and finish a controlled
  full-text pass for those selected materials.
- Governed source of truth: `kx/sql/002_issue_perimeter.sql`, `kx/src/radar_kx/issue_perimeter.py`,
  `kx/scripts/export_v2_perimeter.py`, and the production evidence under
  `/root/radar-kx-issue-perimeter-20260821T175510Z` on Local Ru.
- Affected files: additive KX schema, KX CLI/database/fetch/parser code, focused KX tests, and this
  report. Radar V2 runtime, active V2 content release, Caddy, DNS and firewall are out of scope.
- GRACE module delta: skip - Radar has no `M-*`/`V-M-*` module map. Affected files, invariants,
  tests, production evidence and residual gaps are recorded here.

## Agreed Product Plan

1. Source-of-truth layer: keep immutable raw HTTP bodies, canonical document versions, chunks and
   exact evidence spans in PostgreSQL. Treat embeddings, extracted claims, entities, graph edges,
   rankings and answer packets as derived and rebuildable.
2. Editorial perimeter layer: import every active Radar V2 `issue_materials` selection as an
   immutable `issue_perimeter_sources` + `issue_perimeter_members` snapshot, preserving the nested
   source rows and flattened fields used by KX.
3. Retrieval layer: ship PostgreSQL lexical search first, then add pgvector multilingual embeddings
   and RRF fusion. Add reranking only after a measured improvement on a gold question set.
4. Extraction layer: run LangExtract behind an adapter over canonical text only. Accept only
   exact-span evidence; null/fuzzy spans stay candidates and never enter factual answers.
5. Answer layer: LLM returns IDs and structure, while deterministic renderer/verifier injects
   quotes, numbers, units, dates and links from KX. Strict mode returns insufficient-data instead of
   unsupported prose.
6. Graph/ranking layer: start with SQL nodes/edges tied to `claim_id -> evidence_id`, entity merges
   as reversible decisions, Cytoscape.js for bounded subgraphs, and versioned explainable idea
   scoring.

## Schema and Contract Delta

- `issue_perimeter_sources` stores one audited source artifact, currently active V2 release
  `rel_1c420e848b99357be9b53106`.
- `issue_perimeter_members` stores each issue/material selection with issue metadata, material
  metadata, editorial fields, nested original rows, payload SHA-256 and document link.
- `issue_perimeter_documents` summarizes distinct selected documents and their complete-text state.
- `fetch_queue` now has per-document `robots_override`, `robots_override_reason` and
  `body_limit_bytes`. Overrides require a reason and are scoped by code to issue-perimeter gaps.
- `network_robots_override` is a distinct source kind in attempts and document versions, so
  owner-approved fetches cannot be mistaken for ordinary robots-respecting fetches.
- `reparse_runs` records derived parsing of already retained raw blobs; truncated legacy excerpts
  are excluded from reparse completion.

## Production Evidence

Active final KX release:
`/opt/radar-kx/releases/radar_kx_release_20260821_issue_perimeter_cb2bd80f030c`.

Retained evidence directory:
`/root/radar-kx-issue-perimeter-20260821T175510Z`.

Pre-change backup:
`/root/radar-kx-issue-perimeter-20260821T175510Z/radar_kx_pre_issue_perimeter.dump`, SHA-256
`b0576151a04190bf5b0c819bd12612955c361d4478d564e49b0d363301741ce3`.

Final retained backup:
`/var/backups/radar-kx/20260821T183209Z/radar_kx.dump`; checksum and `pg_restore --list` passed.
Restore-check database `radar_kx_restorecheck_20260821t183209z` passed full KX verification with
`errorCount=0`.

Final production KX totals:

- 8,310 materials and 15,963 material revisions;
- 8,313 documents;
- 6,654 raw blobs;
- 6,767 document versions;
- 6,464 complete versions;
- 5,954 documents with a complete best version;
- queue: 5,908 succeeded, 2,403 failed, zero pending/retry/running.

Issue-perimeter totals:

- 1 perimeter source;
- 48 issues;
- 269 issue/material rows;
- 269 distinct selected materials;
- 267 distinct selected documents;
- 244 complete selected documents;
- 23 selected documents still unavailable after bounded retries;
- all 23 remaining gaps carry an explicit owner-approved robots override flag and reason.

The controlled completion pass requeued 18 failed perimeter gaps, relaxed the body limit to
50 MiB for 23 gaps, and made the robots override auditable for those 23 only. Additional bounded
retry passes exhausted the five retryable network/timeout rows. No authentication, paywall,
CAPTCHA, credential, private-network or security bypass was used, and no title/summary metadata was
promoted to full text.

## Remaining Unavailable Documents

After the override and retries, 23 selected documents remain incomplete:

- 15 HTTP 403 responses;
- 4 timeouts;
- 2 weak or missing article text despite HTTP 200/raw evidence;
- 1 HTTP 404;
- 1 TLS certificate verification failure.

These are terminal in the current policy. They need either a legitimate alternate source artifact,
manual source copy provided by the operator, a source-specific parser backed by retained raw HTML,
or an explicitly approved credentials/subscription path.

## Verification

- Local `kx/scripts/verify.sh`: 49 tests, Ruff format, Ruff lint/import order, strict mypy and
  locked requirements check passed.
- Production `radar_kx verify --full`: `status=ok`, `errorCount=0`.
- Restored backup verification: `status=ok`, `errorCount=0`.
- Final backup checksum and TOC passed.
- `radar-kx-ingest.timer` and `radar-kx-backup.timer` remain enabled and active; their services are
  inactive after successful/idle completion.
- Radar V2 active content pointer remains
  `rel_1c420e848b99357be9b53106` /
  `617ab2c54f1fc5387adeec2c77c132ec18d579ce2f7991cb25347bd40fce21ca`.
- Radar V2 service PID remains `70308`, PostgreSQL PID `1124`, Caddy PID `1025`; all have
  `NRestarts=0`.
- Public `https://radar.agpm.space/api/health` returns the unchanged health body SHA-256
  `041f654dbcca2bfd5246474f04ba072089669f0bf936e22add5b2531c3cd6990`.

## Next Acceptance Gates

1. ADR: freeze the evidence/answer contract, strict factual mode and unsupported-claim behavior.
2. Schema v3: add extraction job contracts for LangExtract runs, claim candidate review states,
   entity resolution decisions and graph edge provenance.
3. Retrieval acceptance: 50-100 Russian/mixed questions with gold evidence, Recall@10 target and
   latency/cost budget.
4. Extraction acceptance: exact-span coverage, manual precision sample, model cost per document and
   retry/refusal accounting.
5. Answer acceptance: zero unsupported claims and zero numeric quote drift in strict mode.
6. UI acceptance: internal search/evidence viewer first, then graph/ranking surfaces over bounded
   server-selected subgraphs.
