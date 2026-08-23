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


#: What a hybrid search may be narrowed by. Every one of these is a column the
#: reading pass (stage 0b) filled, and the point of the filter is the same as the
#: point of the label beside a quotation: a reader asking about knowledge should
#: not be answered out of the market chronicle, and one asking what the canon says
#: should not be answered out of a vendor's blog.
FILTERS = ("admission", "material_kind", "status", "topic_key")

#: The three ways a quotation can be found. Kept as names rather than as booleans
#: because they are shown to the reader - "why was this found" is UC-01's own
#: question, and "matched your words" and "means something close" are different
#: answers.
ARMS = ("слова", "смысл")

#: How deep the semantic arm reaches before fusion. Cosine ranks everything, so
#: without a cut the arm contributes a full ranking of the corpus and RRF quietly
#: turns into "whatever the embedder thinks", drowning two precise lexical lists.
SEMANTIC_DEPTH = 50

EVIDENCE_SQL_TEMPLATE = """
WITH scope_documents AS (
    {scope}
),
asked AS (
    SELECT replace(
               plainto_tsquery('pg_catalog.russian', %(question)s)::text, ' & ', ' | '
           )::tsquery AS ru,
           replace(
               plainto_tsquery('pg_catalog.english', %(question)s)::text, ' & ', ' | '
           )::tsquery AS en
),
scoped AS (
    SELECT evidence.claim_id,
           evidence.quote_text,
           evidence.char_start,
           evidence.char_end,
           documents.canonical_url,
           reading.material_kind,
           reading.admission,
           reading.primary_source,
           reading.is_retelling,
           dates.published_on,
           dates.shown_on,
           dates.shown_kind,
           status.status
    FROM kx.claim_evidence AS evidence
    JOIN kx.document_versions AS versions USING (version_id)
    JOIN kx.documents AS documents USING (document_id)
    JOIN scope_documents USING (document_id)
    LEFT JOIN kx.claim_reading AS reading ON reading.claim_id = evidence.claim_id
    LEFT JOIN kx.document_dates AS dates ON dates.document_id = documents.document_id
    LEFT JOIN kx.knowledge_status_current AS status
           ON status.unit_kind = 'claim' AND status.unit_id = evidence.claim_id
    WHERE evidence.match_status = 'exact'
      AND (%(admission)s::text IS NULL OR reading.admission = %(admission)s::text)
      AND (%(material_kind)s::text IS NULL OR reading.material_kind = %(material_kind)s::text)
      AND (%(status)s::text IS NULL OR status.status = %(status)s::text)
      AND (
          %(topic_key)s::text IS NULL
          OR EXISTS (
              SELECT 1 FROM kx.claim_topics AS placed
              JOIN kx.topics AS topics USING (topic_id)
              WHERE placed.claim_id = evidence.claim_id
                AND topics.topic_key = %(topic_key)s::text
          )
      )
),
ranked_ru AS (
    SELECT scoped.claim_id,
           row_number() OVER (
               ORDER BY ts_rank(
                   to_tsvector('pg_catalog.russian', scoped.quote_text), asked.ru
               ) DESC, scoped.claim_id
           ) AS position,
           'слова' AS arm
    FROM scoped, asked
    WHERE to_tsvector('pg_catalog.russian', scoped.quote_text) @@ asked.ru
),
ranked_en AS (
    SELECT scoped.claim_id,
           row_number() OVER (
               ORDER BY ts_rank(
                   to_tsvector('pg_catalog.english', scoped.quote_text), asked.en
               ) DESC, scoped.claim_id
           ) AS position,
           'слова' AS arm
    FROM scoped, asked
    WHERE to_tsvector('pg_catalog.english', scoped.quote_text) @@ asked.en
),
ranked_meaning AS (
    SELECT claim_id, row_number() OVER (ORDER BY distance) AS position, 'смысл' AS arm
    FROM (
        SELECT scoped.claim_id,
               vectors.embedding <=> %(question_vector)s::vector AS distance
        FROM scoped
        JOIN kx.text_embeddings AS vectors
          ON vectors.owner_kind = 'claim_evidence'
         AND vectors.owner_key = scoped.claim_id::text
         AND vectors.model_id = %(embedding_model)s
        WHERE %(question_vector)s::text IS NOT NULL
        ORDER BY distance
        LIMIT %(semantic_depth)s
    ) AS nearest
),
ranked AS (
    SELECT * FROM ranked_ru
    UNION ALL SELECT * FROM ranked_en
    UNION ALL SELECT * FROM ranked_meaning
),
fused AS (
    SELECT claim_id,
           sum(1.0 / (%(rrf_k)s + position)) AS relevance,
           array_agg(DISTINCT arm ORDER BY arm) AS matched_by
    FROM ranked GROUP BY claim_id
)
SELECT scoped.claim_id,
       scoped.quote_text,
       scoped.char_start,
       scoped.char_end,
       scoped.canonical_url AS source_url,
       scoped.material_kind,
       scoped.admission,
       scoped.primary_source,
       scoped.is_retelling,
       scoped.published_on,
       scoped.shown_on,
       scoped.shown_kind,
       scoped.status,
       fused.relevance,
       fused.matched_by,
       (
           SELECT array_agg(topics.title ORDER BY topics.title)
           FROM kx.claim_topics AS placed
           JOIN kx.topics AS topics USING (topic_id)
           WHERE placed.claim_id = scoped.claim_id
       ) AS topics
FROM fused JOIN scoped USING (claim_id)
ORDER BY fused.relevance DESC, scoped.claim_id
LIMIT %(limit)s
"""


def evidence_sql(scope: str) -> str:
    """The hybrid retrieval: two lexical rankings, one by meaning, fused by rank.

    Reciprocal rank rather than a weighted score, for the reason RRF exists: the
    three arms produce numbers that are not comparable - `ts_rank` is a term
    density, cosine distance is an angle - and any weighting of them would be a
    constant somebody chose. Rank is the only thing all three agree on.

    The semantic arm disappears cleanly when there is no question vector: without
    torch in the runtime there is no way to embed a question, and the NULL check
    leaves the search exactly as lexical as it was before.

    Every bare parameter carries a cast. PostgreSQL cannot infer the type of a
    placeholder that only ever appears beside `IS NULL`, and refuses the whole
    query with `could not determine data type` - which is a runtime error in the
    one code path that has no test able to reach a database.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown search scope {scope!r}; expected one of {sorted(SCOPES)}")
    return EVIDENCE_SQL_TEMPLATE.format(scope=SCOPES[scope].strip())


#: The same three-armed retrieval, over the public surface rather than over `kx`.
#: A second query rather than a parameter on the first, because the two read
#: different things on purpose: this one can only see `agent.*`, and that is what
#: the serving role is allowed to reach (migration 024).
AGENT_SEARCH_SQL = """
WITH asked AS (
    SELECT replace(
               plainto_tsquery('pg_catalog.russian', %(question)s)::text, ' & ', ' | '
           )::tsquery AS ru,
           replace(
               plainto_tsquery('pg_catalog.english', %(question)s)::text, ' & ', ' | '
           )::tsquery AS en
),
scoped AS (
    SELECT statement.*
    FROM agent.statement AS statement
    WHERE (%(admission)s::text IS NULL OR statement.admission = %(admission)s::text)
      AND (%(material_kind)s::text IS NULL OR statement.material_kind = %(material_kind)s::text)
      AND (%(status)s::text IS NULL OR statement.status = %(status)s::text)
      AND (
          %(topic_key)s::text IS NULL
          OR EXISTS (
              SELECT 1 FROM agent.statement_topic AS placed
              WHERE placed.claim_id = statement.claim_id
                AND placed.topic_key = %(topic_key)s::text
          )
      )
),
ranked_ru AS (
    SELECT scoped.claim_id,
           row_number() OVER (
               ORDER BY ts_rank(
                   to_tsvector('pg_catalog.russian', scoped.quote_text), asked.ru
               ) DESC, scoped.claim_id
           ) AS position,
           'слова' AS arm
    FROM scoped, asked
    WHERE to_tsvector('pg_catalog.russian', scoped.quote_text) @@ asked.ru
),
ranked_en AS (
    SELECT scoped.claim_id,
           row_number() OVER (
               ORDER BY ts_rank(
                   to_tsvector('pg_catalog.english', scoped.quote_text), asked.en
               ) DESC, scoped.claim_id
           ) AS position,
           'слова' AS arm
    FROM scoped, asked
    WHERE to_tsvector('pg_catalog.english', scoped.quote_text) @@ asked.en
),
ranked_meaning AS (
    SELECT claim_id, row_number() OVER (ORDER BY distance) AS position, 'смысл' AS arm
    FROM (
        SELECT scoped.claim_id,
               vectors.embedding <=> %(question_vector)s::vector AS distance
        FROM scoped
        JOIN kx.text_embeddings AS vectors
          ON vectors.owner_kind = 'claim_evidence'
         AND vectors.owner_key = scoped.claim_id::text
         AND vectors.model_id = %(embedding_model)s
        WHERE %(question_vector)s::text IS NOT NULL
        ORDER BY distance
        LIMIT %(semantic_depth)s
    ) AS nearest
),
ranked AS (
    SELECT * FROM ranked_ru
    UNION ALL SELECT * FROM ranked_en
    UNION ALL SELECT * FROM ranked_meaning
),
fused AS (
    SELECT claim_id,
           sum(1.0 / (%(rrf_k)s + position)) AS relevance,
           array_agg(DISTINCT arm ORDER BY arm) AS matched_by
    FROM ranked GROUP BY claim_id
)
SELECT scoped.claim_id,
       scoped.statement,
       scoped.quote_text,
       scoped.char_start,
       scoped.char_end,
       scoped.source_url,
       scoped.source_title,
       scoped.material_kind,
       scoped.admission,
       scoped.primary_source,
       scoped.is_retelling,
       scoped.shown_on,
       scoped.shown_kind,
       scoped.status,
       fused.relevance,
       fused.matched_by,
       (
           SELECT array_agg(placed.title ORDER BY placed.title)
           FROM agent.statement_topic AS placed
           WHERE placed.claim_id = scoped.claim_id
       ) AS topics
FROM fused JOIN scoped USING (claim_id)
ORDER BY fused.relevance DESC, scoped.claim_id
LIMIT %(limit)s
"""
