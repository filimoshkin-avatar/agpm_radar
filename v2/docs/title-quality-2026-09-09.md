# Material title quality: incident, data flow and delivery

## Confirmed before changes

The tracked worktree was clean at `b02dd73931ce529679ce2488f3bd1418d41198e7`.
Two unrelated untracked parent documentation files were retained untouched.
The repository has no GRACE knowledge graph, verification plan, operational packets
or module contracts for these Python modules; their existing function/module
boundaries and the root AGENTS.md govern this change.

1. `pipeline/scripts/agpm_radar_collect.py`: `collect_brave`, `collect_perplexity`
   and `collect_openclaw_cli` accept provider `title`, substitute the query when
   absent, and build summaries. `update_materials` derives identity, summary and
   perimeter and saves `data/materials.jsonl`, recording `source_hits`.
2. The collection job actually invokes mirrored scripts in
   `/root/.openclaw-projectmanager/workspace/scripts`. The corpus there is copied
   to `/mnt/vdd/Radar/data/corpus/knowledge-agpm-radar` after reporting.
3. `agpm_radar_report.py` merges the 30-day candidate window with
   `daily-deferred.jsonl`, removes previously reported events, checks web links,
   enriches selected items, classifies/deduplicates, selects the batch, writes the
   deferred queue, then Markdown and DOCX. Before this change,
   `page_title_mismatch` exempted titles with fewer than four significant tokens
   and compared with the union of all page title words. Fulltext caches held
   body/excerpt but no title evidence; enrichment never changed `title`.
4. `agpm_radar_docx_backfill.py` parses Markdown/DOCX headings into `ParsedMaterial`,
   derives `brief`, and imports Legacy SQLite. Its HTML metadata extractor stores
   a document title separately in `source_metadata`, without repairing the card.
5. `agpm_radar_llm_classify.py`, `agpm_radar_issue_theses.py` and
   `agpm_radar_openclaw_analysis.py` consume the imported title/summary/brief.
   `agpm_radar_quality.py` rebuilds Legacy FTS from those fields;
   `agpm_radar_site_export.py` exports them to the public JSON capture.
6. `tools/build_stage14_daily.py::_material` copied `raw['title']` into the V2
   candidate. The candidate and public validators checked only type/length.
   V2 renderers, public API search and DOCX consume the accepted aggregate.

The 2026-09-09 incident passed every one of these boundaries as `User`.
The earlier correction `rel_dc39044376c9b7d9834a285a` fixed the title, but an
additional read-only audit found `summary` and `brief` still quoting `User`.

## Policy and recovery

`packages/contracts/title_quality.py` is the single policy implementation,
shared by Legacy, candidate validation, public validation, API and migration
artifacts. No external parsing dependency enters the public runtime.

- Normalize Unicode NFKC, HTML entities, case, spaces and surrounding punctuation
  for comparison. Reject dialogue roles and common role/message wrappers, empty
  values, URL titles, technical placeholders, status/error/challenge pages and
  obviously meaningless strings. No general minimum word count is imposed.
- Extract Article-family JSON-LD headline/name (including graphs), og:title,
  twitter:title, h1 and title, independently of HTML attribute order.
- Trust order: article headline, Open Graph, Twitter, article name, h1, title.
  Repeated identical values count once per source. Independent agreement breaks
  ties within a tier; unresolved same-tier conflicts cannot authorize repair.
  A lone h1 or document title is insufficient. Organization/WebSite names do
  not become article names. Strip only a corroborated final site suffix.
- Compare with the chosen reliable candidate even for short input titles.
  Less than 45% overlap relative to the larger normalized token set is a
  substantial mismatch. Never merge unrelated candidates' token sets.
- Repair provider titles before summary normalization, identity, perimeter and
  saving. Also repair existing stored bad titles on rediscovery. Report checks
  run before title-dependent deduplication/selection and rendering. Fulltext
  caches retain versioned title evidence and the original source; body-only old
  caches are refreshed. Suspect values without reliable evidence raise
  `TITLE_QUALITY_GATE` with reason, URL and bounded JSON-escaped value.
- Preserve good text; replace exact or explicitly quoted references to the old
  title. Record `title_quality` in Legacy material/source-hit provenance and in
  a report `.title-quality.json` sidecar. V2 additionally rejects stale explicit
  article references in material and issue prose and suspect evidence titles.
- History is not silently rewritten during an audit. In particular, the daily
  full historical DOCX reimport is not converted into an unscoped new gate.
  New reports and the independent V2 publication boundary enforce the policy.

## Audit scope and findings

Read-only audit of the active source snapshot and Legacy corpus, 2026-09-09:

| Scope | Count | Findings |
|---|---:|---|
| Workspace materials.jsonl | 11,052 | `User`, `No title`, domain-shaped `dair.ai` |
| Mirrored corpus materials.jsonl | 11,052 | Same three records |
| Legacy deferred queue | 1 | None |
| Legacy SQLite materials | 424 | Cora `User`, retained historical input |
| V2 materials | 433 | Three URL titles, not linked to any issue or queue |
| V2 published cards | 405 | No bad title; Cora summary/brief retained old reference |
| V2 editorial queue | 129 | No bad title |

Unpublished findings: `c5f1ed7f1364d086` (`dair.ai`, Reddit; may be a brand),
`b5d88f57ebc7377e` (`No title`, incidentdatabase RSS), and V2
`mat_e399aa0155cf713f2dc9cf1b`, `mat_a70f3e4e25108bbd7cd78107`,
`mat_5f571fbc7ba895bb3a2346ae` (two VentureBeat URLs and one Gartner URL).
These are audit findings, not authorization to add/remove cards or edit history.
They must pass recovery if selected in future. The Cora corpus record is
`475c7d8815dc3608`; Legacy SQLite id is `4ce4d4ed6f8e14fb`.

## Verification and rollout

Tests cover normalized roles, short valid titles, exact captured Cora metadata,
JSON-LD and social metadata, conflict ranking, site suffixes, soft errors,
no-candidate failure, rediscovery, old and new fulltext caches, good LLM text,
Markdown/DOCX, unchanged composition/fields, and independent V2 bypass gates.
The mandatory V2 pytest suite invokes the dependency-free unittest runner for
Legacy tests with the system Python used by Legacy itself.

Required gates: V2 Ruff format/lint, strict mypy, pytest, contract validation,
JS syntax and frontend smokes, cache tokens, design debt, isolation, production
artifact; both KX verification scripts required by AGENTS.md. An independent
second-agent review is required before release.

Rollout order:

1. Back up source DB/pointer and Legacy runtime; remotely back up immutable
   content DB, active pointer and API/web/activator code. Verify hashes, SQLite
   integrity and foreign keys. Retain old code and content releases.
2. Use the standard correction package/publisher to replace only Cora's stale
   `summary`/`brief` references. Keep its already corrected title. Assert full
   database row parity except these fields, dependent content hashes and normal
   correction audit timestamps/LLM-attempt metadata.
3. Commit reviewed code and build the application release from the clean commit;
   deploy API then web with the standard release script. Install matching
   activator code including the new shared runtime dependency, under mutation lock.
4. Mirror the reviewed Legacy scripts after verifying the live copies still match
   their backed-up preconditions; include the new adapter in the mirror gate.
5. Check public health/API/issue route/search/DOCX, service NRestarts, release and
   state hashes, source/production parity, byte manifests and retained rollback
   targets. Application-only activation must not change content state.

Rollback uses retained API/web/activator symlink targets and the standard content
publisher's optimistic pointer rollback if needed; restore the backed-up Legacy
scripts. No active SQLite file is edited in place and no migration is required.

Limitations: semantic mismatch detection is conservative lexical evidence, not
an LLM judgment. A plausible title on an unavailable page cannot be proven wrong;
obvious roles/placeholders still fail closed. A previously unseen provider/error
wrapper may need another policy regression. Ambiguous HTML blocks a suspect
input rather than inventing a headline. `dair.ai` remains a reviewed ambiguity.

Verified before application activation:

- V2 `scripts/verify.sh`: PASS; 301 pytest tests, Ruff, strict mypy (106 files),
  all JS checks/smokes, contracts, asset tokens, isolation and deterministic
  production artifact (`af7d63aee7d84ccb3fb917a5a5d66caee7cc88c03a78cfa9f06a773f80cd19c6`).
- KX `verify.sh`: PASS, 575 passed / 202 skipped (expected server-dependent tests).
  KX `verify_migrations.sh`: PASS, complete 777-test suite on temporary local DBs.
- Independent review: six findings addressed; follow-up review found no blockers.
- Source backup: `/tmp/radar-title-backup-20260909`, manifest `sha256.json`,
  DB SHA-256 `48c07710f965b3f5630e7b7431fcfb6e93712f1bd12de6c5aa8370c325031b41`.
- Production backup: `/var/backups/radar-v2/title-quality-20260909`, manifest
  `sha256.json`, DB SHA-256
  `151f64747d663751a76c47dbc36a5cc51d03d065229598b5e1fc0364b85fed32`.
  Both pointers hash to
  `30ccea83d8ca85da1abdc160f30817c540e3dd4b7681de1899790a74e15123e6`;
  both logical states are
  `64384bea608ef1a3342e656aff4b19b7bd775b45ebaa7893b3f3c5c98e62abce`.
  Different DB file hashes reflect independent replicas, not content drift.
- Retained correction review/package:
  `/tmp/radar-title-coherence-correction/reviewed-changes.json` and `packages/`.
  All 95 public issue projections / 405 cards validate in the staged result.
