BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Research answers, their evidence and their refusals (slice 2.14)
--
-- ADR-0004 §9: when there is no basis, the answer is a **structural refusal, not
-- a hedged sentence**. "Probably", "it appears that" and "sources suggest" are
-- ways of publishing an unsupported claim while sounding careful. So a refusal is
-- a row with a reason code, not a paragraph that reads like an answer.
--
-- §10: the internal reason code is always precise even where the outward wording
-- is a policy of the scope - `no_evidence` when the fact is not in the base at
-- all, `out_of_scope` when it is there and not reachable from the asker. Kept
-- apart now rather than later, because refusal semantics harden into the gold
-- sets and changing them afterwards means changing those too.
--
-- ADR-0006 §10: the cache key is **(normalized question, scope, release_id)**. A
-- cache without scope in the key moves content between access levels, and it does
-- it silently. The unique index below is that rule, written where it cannot be
-- forgotten.
-- ---------------------------------------------------------------------------

CREATE TABLE research_answers (
    answer_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The three parts of the cache key.
    normalized_question text NOT NULL CHECK (length(normalized_question) > 0),
    scope text NOT NULL CHECK (scope IN ('public', 'research', 'editor')),
    release_id text REFERENCES knowledge_releases(release_id),

    question text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('strict', 'research')),

    -- Either an answer or a refusal, never both and never neither.
    answer_text text,
    refusal_reason text CHECK (
        refusal_reason IS NULL OR refusal_reason IN ('no_evidence', 'out_of_scope')
    ),
    -- What the base does support nearby (§9a). Retrieved for the *question* and
    -- rendered first and separately, never merged into the refusal.
    adjacent_support jsonb NOT NULL DEFAULT '[]'::jsonb,

    -- What was checked, and what passed. Stored so a later reader does not have
    -- to trust that it was.
    verification jsonb NOT NULL,
    evidence_package jsonb NOT NULL,
    clause_count integer NOT NULL DEFAULT 0 CHECK (clause_count >= 0),
    bound_clause_count integer NOT NULL DEFAULT 0 CHECK (bound_clause_count >= 0),

    model text,
    prompt_sha256 char(64) CHECK (prompt_sha256 IS NULL OR prompt_sha256 ~ '^[0-9a-f]{64}$'),
    answered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    answered_by text NOT NULL,

    CONSTRAINT an_answer_or_a_refusal CHECK (
        (answer_text IS NULL) <> (refusal_reason IS NULL)
    ),
    CONSTRAINT bound_clauses_are_a_subset CHECK (bound_clause_count <= clause_count)
);

-- ADR-0006 §10, as an index rather than as a convention.
CREATE UNIQUE INDEX research_answers_cache_key
    ON research_answers (normalized_question, scope, coalesce(release_id, ''));

CREATE INDEX research_answers_refusals_idx ON research_answers (refusal_reason, answered_at DESC)
    WHERE refusal_reason IS NOT NULL;

CREATE TRIGGER research_answers_immutable
BEFORE UPDATE OR DELETE ON research_answers
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

GRANT ALL ON research_answers TO radar_kx;

UPDATE metadata SET value = '17'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
