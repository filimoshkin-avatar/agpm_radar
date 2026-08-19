# Radar V2 Stage 3: SQLite and Legacy importer evidence

Date: 2026-08-19

Status: accepted after independent review; Stage 4 was not started

## Scope and safety

Stage 3 changed only `v2/**` and this evidence document. Legacy runtime code, contracts,
production SQLite, cron, services, deployment state, Local Ru and DNS were not changed.

The real Legacy database was opened only after the synthetic suite passed. It was opened with
SQLite URI `mode=ro&immutable=1`, `PRAGMA query_only=ON`, and was SHA-256 checked before and after
the import. The V2 target and replica were created under `/tmp` and were disposable. The Legacy
database remained byte-identical:

- Legacy DB SHA-256 before/after:
  `481d5d6c9b54a58f78f288fb29c0eb072d43e74d6c2db8b14044a3153cd8f7f7`;
- frozen evidence-manifest SHA-256:
  `9f6c488bbddd2975fa89a75d35348990814a85f79dcee0bd15a2fa513043f121`;
- deferred-queue input SHA-256:
  `832d939f77857a5aaf9f81a19dc9ae962da19ba46b87a76b316a734d085604bf`;
- Legacy gazette asset SHA-256:
  `1e6ba2bb055a2821bca2e05ad7ef6ec57e3a558049875ffc5e601c58911b637d`.

## Implementation

The Stage 3 implementation provides:

- a lexical, checksum-pinned migration runner using `BEGIN IMMEDIATE` and explicit
  `applied_at` values;
- a strict V1 schema for every contract table, column, primary/unique key, foreign key,
  supporting index, lifecycle check, public view and FTS5 projection;
- deterministic IDs derived from namespace plus stable Legacy keys;
- immutable publication provenance and seven evidence records per historical issue;
- normalized `issue_materials` instead of destructive/global material ownership;
- a bootstrap-only importer which fails closed once sequence-zero `content_releases` exists;
- safe non-public representation of deferred and metadata-only materials;
- public view filtering at the SQLite boundary and deterministic FTS rebuilding;
- canonical per-table and aggregate logical hashing;
- a read-only source/replica equivalence CLI;
- a host-local, append-only, hash-chained and fsynced publisher audit JSONL journal with
  inter-process `flock`, no-follow/regular-file checks, private permissions and post-append chain
  verification; it is not stored in replicated SQLite.

Migration `0001_initial.sql` SHA-256:
`3762ffd9aa9b22f8a0ae0a5f7225f05787f9f440da8a0fc6ff32a8131aae2ca9`.

The real import command used the module entry point:

```bash
cd v2
uv run --no-sync python -m tools.legacy_import \
  --legacy-db ../data/db/radar.sqlite \
  --target-db /tmp/<disposable>/radar-v2-import.sqlite \
  --evidence-manifest ../fixtures/legacy-baseline/all-issues-evidence.json \
  --evidence-manifest-sha256 9f6c488bbddd2975fa89a75d35348990814a85f79dcee0bd15a2fa513043f121 \
  --imported-at 2026-08-19T12:00:00Z \
  --deferred-queue ../data/corpus/data/daily-deferred.jsonl \
  --gazette-asset ../work/radar-app/gazette-20260803.html \
  --gazette-relative-path gazettes/gazette-20260803.html \
  --gazette-period 2026-08 \
  --gazette-title 'AgPM Gazette August 2026' \
  --gazette-published-at 2026-08-03T00:00:00Z
```

## Real import gate

- inferred historical publications: **74**;
- ambiguous historical drafts: **0**;
- all 74 retained Legacy `status='draft'` and `published_at=NULL` as provenance;
- public issues/materials/stats: **74 / 254 / 74**;
- public search projection and FTS rows: **254 / 254**;
- explicit empty issues preserved: **28**, including `2026-07-26`;
- queue states: **122 review**, **6 deferred**, **0 manual**;
- unassigned non-public materials: **26** (six deferred plus metadata-only rows, with four URL
  overlaps); total materials: **280**;
- safe source metadata: **244/244** rows represented as `material_evidence`;
- stored Legacy outcome records: **483** = 254 classifications + 74 daily analyses + 4 issue
  theses + 148 period analyses + 3 material summaries; **472** deterministic rule/fallback rows
  are recorded as `skipped` with `LEGACY_DETERMINISTIC_FALLBACK`, while **11** accepted provider
  model calls are recorded as `success`;
- gazettes/assets: **1 / 1**;
- `integrity_check=ok`, zero foreign-key violations;
- `application_id=1380009010`, `user_version=1`, journal mode `delete`;
- no WAL/SHM/journal sidecars;
- zero forbidden local-path fragments across replicated text columns;
- release sequence: **0**; stored release `after_state_hash` equals the computed state hash.

Logical state SHA-256:
`ef5b4c3ef7ddfcda05c5aad331043bcc576ec641683e05d74ce1162e1e7c7f41`.

Disposable SQLite artifact evidence:

- mode: `0600`;
- bytes: `4,898,816`;
- file SHA-256:
  `e285e439df3ebaef777b35e7e26b1a49c89a99f5ce8a0db7988310a6af906f1c`.

A separate SQLite backup was compared with `python -m tools.compare_databases`. Source and
replica had the same aggregate state hash, every table count, every table hash and the complete FTS
projection hash; mismatch maps were empty. Both FTS indexes passed content equality and an FTS5
integrity check. File SHA-256 is intentionally not the logical equality mechanism.

The complete real import was also repeated into a second fresh database with the same explicit
inputs. Both artifacts were byte-identical with file SHA-256
`e285e439df3ebaef777b35e7e26b1a49c89a99f5ce8a0db7988310a6af906f1c`, and the equivalence report
again had empty count/hash mismatch maps. Re-running against an already sealed target fails closed;
the synthetic regression asserts `BootstrapSealedError`.

The complete FTS projection SHA-256 on both artifacts was
`b1a473014506e310a621a9df09ccf8da5fd63b8b6eca436d59808695a97e7cda`.

## Contract-table coverage ledger

`content_releases.after_state_hash` is excluded from its per-table hash as required by the
contract. FTS virtual/internal tables are derived and excluded from canonical hashing.

| Contract table | Source / derivation | Rows | Canonical table SHA-256 | Allowed-empty evidence |
|---|---|---:|---|---|
| `application_compatibility` | explicit Stage 3 compatibility marker | 1 | `504d7461166fddedb9ff59acdfd16b53ed915bde56c04c0f2cd88fd7ccbd2b27` | non-empty required |
| `content_releases` | immutable bootstrap seal, sequence zero | 1 | `dcc4cb3949bb9bf907a3996b03fd61c39d1a1daae3509b31d649be34754a1511` | non-empty required |
| `daily_stats` | Legacy `daily_stats`, normalized issue key | 74 | `7aedaf4a57260a2115c0dfe59eb85be40e72fe678f90556e2b21ebc1bee4b033` | non-empty required |
| `editorial_queue` | deferred JSONL plus queued `material_date_quality` | 128 | `8cde27d0f8d4c5f28072f930b61ccb24d0046b0194f1520c26954c6dfe2a42d0` | file/queued rows may be empty; Legacy has no separate durable manual queue |
| `gazette_assets` | explicit content-addressed Legacy gazette asset | 1 | `6a9e1fe041270bb45a1c79439514169aac74b1bcccaf729652cad5000ed36ed5` | allowed only when no gazette input exists |
| `gazettes` | explicit Legacy gazette metadata arguments | 1 | `1f1dcfcc224860def8bc505d22e09503698b377708a6ef76fe35c0ac03f54aa0` | allowed only when no gazette input exists |
| `issue_analysis` | daily analysis + 7d/30d period analyses + issue theses | 74 | `ba98a6e316e63f7f22f8b574b55b93386d89f1ebf006f599780f7cf0036bf730` | non-empty required |
| `issue_materials` | normalized `materials.radar_issue_date` membership | 254 | `7456056827c49edfbb49eb63e9039cd8fb6d2b8764b2633db3aa6a918624fa7f` | zero links allowed only for explicit empty issues |
| `issues` | Legacy issues plus frozen inference manifest | 74 | `57754a716d79ea7415fe25239d77b310e6c09a8bf5818088e87126c01ad407f8` | non-empty required |
| `legacy_issue_provenance` | original Legacy lifecycle values | 74 | `3fe584f73386a2ef9b290c8de8249049e154c530fba4861dd717db630bca55e3` | non-empty required |
| `legacy_publication_evidence` | four artifacts + row + integrity + range per issue | 518 | `87db0ae32d75b17666a85d48ac90dba62e5e1c61dc8585cff3c44c3e396294a0` | non-empty required |
| `llm_attempts` | all five Legacy outcome tables; deterministic rules are not model success | 483 | `5f3e0472e956e466480678ae82e35e6cc1524a33edede8607311e195e1c65a01` | allowed only if every Legacy outcome table is empty |
| `material_analysis` | Legacy `material_llm_summaries` | 3 | `81ba8221e8e7b00ccba08d57cf33949133b5010bf3bba2f0e5319d5b588b69a4` | allowed when Legacy summaries are empty |
| `material_evidence` | safe projection of all `source_metadata`; snapshot paths omitted | 244 | `e2b57c2f0c0b61e7fe4c1994dc5e55b8399721362c1bd014bb122a171d40dfe8` | allowed when Legacy source metadata is empty |
| `material_quality` | Legacy `material_date_quality`, issue-normalized | 254 | `c84d716be7f7326e8e53e1753fd589a680db8821c869c196054a696524b401d3` | allowed when Legacy quality is empty |
| `material_rubrics` | Legacy rubric membership, issue-normalized | 728 | `30e32fb65050989b1074a91d2366376ba4c091224145267e2549e70453d7f10d` | allowed when Legacy links are empty |
| `material_sources` | one normalized source membership per material | 280 | `1320146a88dc7e99be75737766f0230294f6424a393b4e9e333640b64a30a973` | non-empty required |
| `materials` | 254 issued + deferred + metadata-only, deduplicated by URL | 280 | `b10ceaaad48db8fafe5c2cf3e71147e025325033ab1d186df646ab2fbbd68b51` | non-empty required |
| `rubrics` | Legacy application vocabulary | 11 | `b3fc05189835714b0bae8c86cafdc7288c16c91922a770fdf325bcbeabc23ecf` | non-empty required |
| `schema_migrations` | checksum-pinned V2 migration runner | 1 | `75ece1026f04f36911e6cf9046fde86398088dc3a5d6bb04bfb11428ab500107` | non-empty required |
| `source_rules` | safe Legacy `source_domain_rules` projection | 6 | `ae589c31fd42a78266649d0b67a6f63b7f3cddd325e85dd634dcb7a8ad854196` | allowed when Legacy rules are empty |
| `source_snapshots` | DB and evidence-manifest aggregate metadata | 1 | `7ab716df9f713fc9bdadf18df3adf0933db7e0446777e8b37bcfa4d1b6b1794d` | non-empty required |
| `sources` | material/source IDs plus deferred and metadata-only derivation | 45 | `7777c0f361e0a73ad8c1dad2fe7f59c5ead53525364d288afeec95479f144794` | non-empty required |

No contract table was empty in the real import, so there are no unexplained empty tables.

Legacy-only `pipeline_runs` was empty. `rejected_materials_internal` is deliberately not a V1
contract table and remains excluded; it is neither publication state nor an editorial queue.
Absolute report, DOCX, request/response, diagnostic and snapshot paths were not replicated.

## Independent acceptance and regressions

Independent review found and corrected five trust-boundary defects before acceptance:

- concurrent audit appenders could fork the hash chain; the journal now serializes the full
  read/verify/append/fsync cycle and rejects symlink or over-permissive targets;
- deterministic Legacy rule output could masquerade as a successful LLM call;
- the stored public API compatibility marker used `v1` instead of exact contract version `1.0.0`;
- replica comparison checked only the FTS row count rather than the full indexed projection;
- database creation inherited a public umask and was not race-safe; it now reserves a new inode
  atomically at mode `0600` and never overwrites an existing path.

Regression evidence:

- Ruff format/lint and strict mypy: pass;
- pytest: **28 passed**;
- Stage 1 contract validator, JavaScript syntax and V2 isolation/secret scan: pass;
- production artifact: **21 runtime files**, deterministic SHA-256
  `3d9eff2783af4438c2d00ef78fe2b933ff74543faa911d11791171bb9a8f78af`;
- audit stress: **12 x 32** concurrent threads and **16 processes / 64 events**, complete chains;
- two full real imports: byte-identical, mode `0600`, with no count/hash/FTS mismatch;
- the active compatibility row contains `1.0.0` for every V1 contract family;
- same-count FTS corruption, audit symlinks, database overwrite and public draft leakage are covered
  by negative regressions.

## Residual risks and boundaries

- Legacy has no authoritative publication timestamps. The 74 inferred issues therefore retain
  `published_at=NULL` exactly as the accepted inference contract requires.
- Legacy has no durable standalone manual queue. The manual queue sub-state is explicitly empty;
  review and deferred states are fully represented.
- The Legacy gazette has no release ledger. Its period/title/publication timestamp are explicit
  bootstrap arguments backed by the frozen asset hash, not recovered authority.
- The importer is intentionally unusable after release zero. Future data changes belong to the
  Stage 7 publisher/delta flow, which was not implemented here.
- The evidence SQLite files under `/tmp` are acceptance artifacts only and are not production
  releases.
