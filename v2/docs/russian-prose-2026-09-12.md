# Russian prose and multilingual source titles

Owner rule, 2026-09-12: translations, summaries and analytical explanations are
Russian. Source-language phrases belong in the article title only. Proper names
may retain their established spelling; names in other scripts use Russian
transcription or an established Latin form. Generic terminology is translated.
Multilingual collection remains enabled.

Incident: PKSHA article `mat_5e418ff2e2b9926d31af48fe`, issue 2026-09-12.
The card contained `и整理ует`; report fallback prose copied the Japanese heading,
and daily analysis and a thesis copied both defects. The previous lexical guard
only recognized lowercase Latin words; the report language heuristic only
compared Latin and Cyrillic counts.

The shared policy is `packages/contracts/russian_prose.py`. Legacy loads it through
`radar_language_quality.py` from both the repository and collection runtime.
Card generation rejects untranslated scripts (including mixed-script words) and
retains its retry feedback. Daily and period analysis apply the shared script and
Latin lexical checks. All generation prompts carry the same language instruction.
Report fallbacks omit source-title references and translate fixed generic terms.
The independent Stage 14 daily boundary rejects untranslated scripts in card prose.
Existing immutable historical releases remain readable under their original contracts.

V2 theses use separate source IDs in the raw model response for grounding; the
public lead/rest shape is unchanged. Unknown IDs and IDs in reader-facing prose
are rejected. The raw response artifact retains per-thesis grounding; the cleaned
public text retains the existing issue-level evidence links and titles.

Limitations: Latin proper-name recognition remains a conservative capitalization
and source-domain heuristic, not a semantic guarantee. Prompts prohibit ordinary
foreign terms and acronyms, including capitalized ones. The pre-existing lexical
article-body binding check still needs shared words/numbers for a card; purely
Japanese sources without those anchors need a separate grounding improvement.
No claim is made that every historical foreign term was rewritten.

Regression coverage includes Japanese, Chinese supplementary characters, Korean,
Arabic, Hebrew, Greek, mixed-script suffixes, Latin generic terms, preserved company
and product names, Russian transcription, Legacy card/report execution, V2 daily
bypass rejection and Russian thesis grounding for a Japanese title. Existing Legacy
card tests also passed. Full V2 verification passed (318 tests); KX verification
passed (575 tests, 202 expected skips). Migration verification runs against temporary
local databases, with no production migration.

Reviewed correction artifacts: `/tmp/radar-language-correction-v5/`.
The staged public diff changes exactly five PKSHA prose fields, two analysis blocks
and one thesis. Source title, evidence titles, article URL, dates, composition and
other cards remain identical. Full database comparison also verifies that remaining
changes are dependent content hashes and normal correction audit metadata.

Delivery uses the standard immutable correction package and optimistic publisher,
with source database/pointer backups. Collection receives the matching report and
language adapter; card generation and V2 analysis execute repository code directly.
No API/web release, service configuration or production migration is needed.
