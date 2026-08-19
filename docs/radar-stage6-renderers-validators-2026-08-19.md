# Radar V2 Stage 6: deterministic renderers and validators

Date: 2026-08-19

Status: accepted after the final gate recorded below.

## Scope and source of truth

Stage 6 implements only the deterministic presentation and validation boundary defined by
`docs/migration-plan-review-2026-08-19.md` § Stage 6. The frozen public contract is
`contracts/v1/public-api.openapi.yaml`; the database/table boundary is
`contracts/v1/sqlite-contract.yaml`; package inputs remain the Stage 5 frozen candidate contract.
No publisher, activation, deployment, cron, service, Caddy, DNS or Local Ru change belongs to this
stage.

Radar has no GRACE `M-*`/`V-M-*` module map. The governed source of truth and affected files were
therefore recorded explicitly before implementation; the commit uses `GRACE-Delta: skip` with that
reason instead of inventing a module identifier.

## Implemented boundary

- `v2/packages/validation/public_issue.py` builds one explicit published-only `IssueDetail` DTO
  from contract SQLite rows. It does not expose tables or draft rows directly.
- `v2/packages/renderers/daily_json.py` produces canonical sorted-key UTF-8 JSON with a final
  newline and rejects non-canonical input on parse.
- `v2/packages/renderers/daily_docx.py` builds deterministic OOXML entirely in memory with Python
  standard-library code: exact members, sorted order, fixed ZIP timestamps, normalized `0644`
  member modes and explicit external HTTP(S) hyperlink relationships.
- `v2/packages/validation/artifacts.py` independently validates JSON and DOCX membership, modes,
  timestamps, XML, relationships, semantic text, links and the expected DTO.
- `v2/packages/validation/gazette.py` verifies the candidate-declared immutable asset set, byte
  counts/hashes, local entrypoint/references and bounded HTML/CSS/SVG safety.
- `v2/fixtures/synthetic/stage6-golden.json` freezes normal, complete no-LLM and gazette inputs plus
  expected JSON/DOCX/text/entrypoint hashes.

The database projection fails closed on inconsistent stats, material-count drift, non-contiguous
ordering, duplicate IDs or canonical URLs, invalid publication dates, lifecycle/draft leakage,
missing aggregate rows, invalid JSON, host paths and secret-shaped values. A missing historical
`material_analysis` row becomes explicit deterministic `fallback` (or `unavailable` when the issue
LLM is unavailable), never an invented model success.

The frozen public API requires second-precision UTC timestamps. Real Legacy data also contains
date-only material timestamps, so only rows belonging to a `legacy_inferred` issue are normalized
to midnight UTC at this projection boundary. Native V2 publications still reject date-only values.

## Determinism and golden acceptance

Synthetic golden hashes:

| Fixture | JSON SHA-256 | DOCX SHA-256 | DOCX text SHA-256 |
|---|---|---|---|
| normal | `bc7cca04e543a9e70e8ed85aa80f88ed2870d360c52b860c03c37ed3a482bc54` | `e3edb76fba86d651bbb8fdb506bb809524e34708450036abd2f23656bcff3fe2` | `8e53e6a61df1b3d8fc463ef647eb8796452cbaec81d51feb05b2674564f9c64c` |
| no LLM | `8e9baa0d0473f0f8fe6df4b15e58fd2ac8e5862cc3e72724cb910e870a6f1d95` | `09085df335cf7c506acb5da291677ed80303d294b59839557f6c992151d61881` | `4fc745a336b7aa0643cdda0b2a8615d8648a6b8a327ea5eed434516e5100a2f7` |

The accepted gazette entrypoint SHA-256 is
`8c3a85df6ce81f6f2d179ffede251e553377a921698ac39c22343fee54abe9f1`.
Repeated renders are byte-identical. The complete no-LLM document remains valid and contains an
explicit outage/fallback notice rather than a false effective model.

## Historical acceptance on a disposable import

A fresh Stage 3 import was built from the unchanged Legacy database SHA-256
`481d5d6c9b54a58f78f288fb29c0eb072d43e74d6c2db8b14044a3153cd8f7f7` and frozen evidence manifest
SHA-256 `9f6c488bbddd2975fa89a75d35348990814a85f79dcee0bd15a2fa513043f121`.
It inferred 74 published issues, zero ambiguous drafts and produced logical state SHA-256
`ef5b4c3ef7ddfcda05c5aad331043bcc576ec641683e05d74ce1162e1e7c7f41`.

Four representative issues were projected, rendered and independently revalidated:

| Date | Materials | LLM | JSON SHA-256 | DOCX SHA-256 | Paragraphs / links |
|---|---:|---|---|---|---:|
| 2026-08-19 | 3 | success | `0cafbbef6fc2abd57d9a7ebec9b464cde9dbc7d9f07f4596043fc7952f39e799` | `f0f494183eb6e61ba580b1985f7f5b0025fca6f7bed957fbf719aaeaebab0063` | 39 / 3 |
| 2026-08-15 | 6 | fallback | `d8238aab9fa1ac5125392afbed2a3cdaca0a6d0987e41d18fe17a59153524b6b` | `9cec0161999863cd60aa849c6ca3048b9b6920b360e60ac23e4f22a863106d5b` | 56 / 6 |
| 2026-08-04 | 16 | fallback | `f8a03d3300e64a68794de6f65cf95fda52e1b0b9e9b925ca7a9cb25d0d142a21` | `7f2ff99161083626a1adb8a9080c0e90f3c16f4cbac5ce10dd687c3a15b8d625` | 116 / 16 |
| 2026-07-26 | 0 | fallback | `85837d1490832ad43d2a9b397a92733aecc47e48ecebf766378afb541dc7ba27` | `e98c1d0dfe8cf6f7644b28e058d600e90094bdf9541b6d93960a5adbeff5dafa` | 18 / 0 |

The historical DOCX comparison also verifies the issue date, viewed count and every material title
against the retained Legacy report for 2026-08-15. The disposable import/evidence remains retained;
no production database was opened for writing.

## Negative regressions

The Stage 6 suite rejects malformed or non-canonical JSON, altered DOCX bytes/membership/modes,
wrong DTO text, duplicate materials, bad stats, invalid dates, draft requests, sparse required rows,
scripted/remote/traversing gazettes, missing gazette assets, CSS imports, secret/host-path leakage
and date-only timestamps on native V2 rows. DOCX DTD/entities, active Word elements, non-allowlisted
relationships and unsafe ZIP members are rejected before semantic acceptance.

The historical acceptance found and fixed two real compatibility defects before commit:

1. an inner join silently omitted 251 of 254 imported issue-material rows because Legacy had only
   three stored material-analysis rows;
2. date-only Legacy material timestamps failed the frozen public date-time contract.

Both now have narrow fail-closed regressions. Neither fix weakens native V2 input rules.

## Final gate

The mandatory command is:

```bash
./v2/scripts/verify.sh
```

Final clean result:

```text
Ruff format: 47 files already formatted
Ruff lint: PASS
strict mypy: 47 source files PASS
pytest: 86 passed
contract validator: PASS (6 schemas, 8 examples, 23 tables, 11 API paths)
JavaScript syntax: PASS
secret/Legacy isolation: PASS (59 files, 3 synthetic fixtures)
production artifact: PASS (36 runtime files)
artifact SHA-256: 4e18b6b47895656ca5814df8130e442fd9ab9541d087112af4140297c5128246
Radar V2 verification: PASS
```

`git diff --check` and focused Stage 6 tests are additional mandatory checks before commit.

## Production non-regression and next boundary

After the final gate, Legacy SQLite remained byte-identical at the exact SHA-256 above.
`radar-api.service` and `caddy.service` were both active/running with `NRestarts=0`; local
`/api/health` returned `ok`, and `pipeline/bin/radar_healthcheck.sh --production` passed for the
latest issue `2026-08-19` with three materials. Stage 6 performed no service reload and no external
mutation.

The next sequential boundary is Stage 7: full seed/delta generation, transactional staging apply,
state/table hashes, idempotent retries, publisher state machine and local disposable activation /
rollback simulation. Stage 7 must not be confused with a production deployment or Local Ru cutover.
