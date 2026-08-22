"""Internal lexical search over KX, and the coverage report that bounds it.

Slice 1.4. Everything downstream - gold sets, retrieval measurement, claim
extraction, the research Q&A mode - needs a way to find a passage and point at
exactly where it is. This is that way, and it is deliberately the boring one:
PostgreSQL full text over the Russian and English indexes that already exist,
fused by reciprocal rank. No embeddings; pgvector is a decision to be taken on
measured evidence after the vertical slice (plan §13.3, 2.16), not before.

Two properties make the results usable as evidence rather than as a reading list:

**Every snippet carries verified offsets.** A hit reports the character range of
its snippet inside ``document_versions.canonical_text``, and the range is checked
against the stored text before it is returned. A snippet whose offsets do not
reproduce it is degraded to one that does, never emitted with offsets that lie.
The exact-span trigger on ``claim_evidence`` will accept nothing less.

**Scope is a membership class, chosen explicitly.** The current issue perimeter,
the union of every perimeter snapshot, the AgPM canon and the whole corpus are
four different questions with four different denominators (corpus-membership
contract §9). Defaulting to "everything" would silently mix them, so the caller
says which one it means and every result says which one answered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Reciprocal-rank-fusion constant. 60 is the value the original RRF paper used
#: and the one every later comparison treats as the default; it flattens the head
#: of each ranking enough that one confident list cannot drown the other.
RRF_K = 60

#: Named membership classes a search may run over. The value is the SQL that
#: yields the document ids of that class.
SCOPES: dict[str, str] = {
    # The newest perimeter snapshot: what the active content release selected.
    "current": """
        SELECT DISTINCT document_id
        FROM kx.issue_perimeter_members
        WHERE perimeter_source_id = (
            SELECT perimeter_source_id
            FROM kx.issue_perimeter_sources
            ORDER BY captured_at DESC
            LIMIT 1
        )
    """,
    # Every snapshot ever taken. Differs from `current` only after a correction
    # removes a material from a published issue (defect D3, still latent).
    "historical": """
        SELECT DISTINCT document_id FROM kx.issue_perimeter_members
    """,
    # The AgPM canon and the external standards.
    "canon": """
        SELECT material_documents.document_id
        FROM kx.material_documents
        JOIN kx.source_materials USING (material_id)
        JOIN kx.corpus_imports USING (corpus_sha256)
        WHERE kx.corpus_imports.source_kind = 'canon_import'
    """,
    # Everything in the store, including the 8038 documents no issue selected.
    "corpus": """
        SELECT document_id FROM kx.documents
    """,
}

_HEADLINE_OPTIONS = 'MaxFragments=0, MaxWords=48, MinWords=20, StartSel="", StopSel=""'

#: How the terms of a query combine.
#:
#: ``all``
#:     every term must appear in one chunk. Right for a quoted phrase, and wrong
#:     for a question: a fifteen-word question conjoined finds nothing, which is
#:     not the same as there being nothing to find.
#: ``any``
#:     any term may match, and ranking decides. Right for a question and for
#:     binding a wiki sentence to a source.
MATCH_MODES = ("all", "any")

#: ``plainto_tsquery`` emits ``'a' & 'b' & 'c'`` with every lexeme already
#: normalized and quoted, so turning the conjunction into a disjunction is a text
#: substitution on a value PostgreSQL produced - never on user input.
_TSQUERY = {
    "all": "websearch_to_tsquery({config}, %(query)s)",
    "any": "replace(plainto_tsquery({config}, %(query)s)::text, ' & ', ' | ')::tsquery",
}

SEARCH_SQL_TEMPLATE = """
WITH scope_documents AS (
    {scope}
),
matched AS (
    SELECT chunks.chunk_id,
           chunks.version_id,
           chunks.char_start,
           chunks.text,
           versions.document_id,
           chunks.search_ru @@ {ru_query} AS ru_hit,
           chunks.search_en @@ {en_query} AS en_hit,
           ts_rank_cd(chunks.search_ru, {ru_query}) AS ru_score,
           ts_rank_cd(chunks.search_en, {en_query}) AS en_score
    FROM kx.chunks AS chunks
    JOIN kx.document_versions AS versions USING (version_id)
    JOIN scope_documents USING (document_id)
    WHERE versions.is_complete
      AND (chunks.search_ru @@ {ru_query} OR chunks.search_en @@ {en_query})
),
ru_ranked AS (
    SELECT chunk_id, row_number() OVER (ORDER BY ru_score DESC, chunk_id) AS position
    FROM matched WHERE ru_hit
),
en_ranked AS (
    SELECT chunk_id, row_number() OVER (ORDER BY en_score DESC, chunk_id) AS position
    FROM matched WHERE en_hit
),
fused AS (
    SELECT matched.*,
           ru_ranked.position AS ru_position,
           en_ranked.position AS en_position,
           coalesce(1.0 / (%(rrf_k)s + ru_ranked.position), 0)
         + coalesce(1.0 / (%(rrf_k)s + en_ranked.position), 0) AS rrf_score
    FROM matched
    LEFT JOIN ru_ranked USING (chunk_id)
    LEFT JOIN en_ranked USING (chunk_id)
)
SELECT fused.chunk_id,
       fused.version_id,
       fused.document_id,
       fused.char_start,
       fused.text,
       fused.ru_position,
       fused.en_position,
       fused.rrf_score,
       documents.canonical_url,
       versions.title,
       versions.language,
       versions.fetched_at,
       ts_headline(
           CASE WHEN fused.ru_hit THEN 'russian' ELSE 'english' END::regconfig,
           fused.text,
           websearch_to_tsquery(
               CASE WHEN fused.ru_hit THEN 'russian' ELSE 'english' END::regconfig,
               %(query)s
           ),
           '{headline_options}'
       ) AS headline
FROM fused
JOIN kx.documents AS documents USING (document_id)
JOIN kx.document_versions AS versions ON versions.version_id = fused.version_id
ORDER BY fused.rrf_score DESC, fused.chunk_id
LIMIT %(limit)s
"""

#: Smoke queries and the floor each has to clear. The corpus grows, so the exact
#: counts of 2026-08-21 are not reproducible and the gate is "no fewer than".
#: A drop means an index, a language configuration or the corpus itself broke.
SMOKE_QUERIES: tuple[tuple[str, str, int], ...] = (
    ("искусственный интеллект", "russian", 993),
    ("artificial intelligence", "english", 633),
)


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: str
    version_id: str
    document_id: str
    canonical_url: str
    title: str
    language: str
    rrf_score: float
    ru_position: int | None
    en_position: int | None
    snippet: str
    #: Character range of the snippet inside the version's canonical text.
    char_start: int
    char_end: int
    #: False when the headline could not be located and the snippet fell back to
    #: the head of the chunk. The offsets are exact either way.
    snippet_is_match_centred: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "chunkId": self.chunk_id,
            "versionId": self.version_id,
            "documentId": self.document_id,
            "canonicalUrl": self.canonical_url,
            "title": self.title,
            "language": self.language,
            "rrfScore": round(self.rrf_score, 6),
            "ruPosition": self.ru_position,
            "enPosition": self.en_position,
            "snippet": self.snippet,
            "charStart": self.char_start,
            "charEnd": self.char_end,
            "snippetIsMatchCentred": self.snippet_is_match_centred,
        }


def locate_snippet(
    chunk_text: str, headline: str, *, fallback_chars: int = 320
) -> tuple[str, int, bool]:
    """Place the headline inside the chunk and return ``(snippet, offset, centred)``.

    ``ts_headline`` normally returns a contiguous run of the input, but it is free
    not to - it collapses whitespace and may drop a fragment. An offset that does
    not reproduce its own text is worse than a coarse one, so a headline that
    cannot be found verbatim is discarded in favour of the head of the chunk,
    which trivially can be.
    """
    candidate = headline.strip()
    if candidate:
        offset = chunk_text.find(candidate)
        if offset >= 0:
            return candidate, offset, True
    return chunk_text[:fallback_chars], 0, False


def build_hit(row: dict[str, Any]) -> SearchHit:
    text = str(row["text"])
    snippet, offset, centred = locate_snippet(text, str(row["headline"] or ""))
    char_start = int(row["char_start"]) + offset
    return SearchHit(
        chunk_id=str(row["chunk_id"]),
        version_id=str(row["version_id"]),
        document_id=str(row["document_id"]),
        canonical_url=str(row["canonical_url"]),
        title=str(row["title"] or ""),
        language=str(row["language"]),
        rrf_score=float(row["rrf_score"]),
        ru_position=None if row["ru_position"] is None else int(row["ru_position"]),
        en_position=None if row["en_position"] is None else int(row["en_position"]),
        snippet=snippet,
        char_start=char_start,
        char_end=char_start + len(snippet),
        snippet_is_match_centred=centred,
    )


def search_sql(scope: str, *, match: str = "all") -> str:
    if scope not in SCOPES:
        raise ValueError(f"unknown search scope {scope!r}; expected one of {sorted(SCOPES)}")
    if match not in MATCH_MODES:
        raise ValueError(f"unknown match mode {match!r}; expected one of {list(MATCH_MODES)}")
    template = _TSQUERY[match]
    return SEARCH_SQL_TEMPLATE.format(
        scope=SCOPES[scope].strip(),
        headline_options=_HEADLINE_OPTIONS,
        ru_query=template.format(config="'russian'"),
        en_query=template.format(config="'english'"),
    )
