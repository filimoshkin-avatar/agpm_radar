BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Embeddings of the things that actually get compared (owner request, 2026-08-23)
--
-- The owner asked for a local embedder and a vector store, so the two ways of
-- linking a wiki statement to evidence can be compared side by side rather than
-- argued about. The lexical way is reciprocal rank fusion over PostgreSQL
-- full-text; the other way is cosine distance between sentence embeddings.
--
-- What gets compared is a **statement** against a **claim's quotation**, so those
-- are what need vectors. `chunk_embeddings` from migration 001 is the wrong grain
-- for that and has never been written to; it is left where it is rather than
-- dropped, because removing a table is a separate decision from not using it.
--
-- One generic table instead of one per owner kind: a claim, a wiki statement and
-- a chunk are the same shape of fact - this text, under this model, is this
-- vector - and one table means one place to ask what has been embedded.
--
-- No index. 13 567 claims at 384 dimensions is 20 MB and an exact scan answers in
-- tens of milliseconds; an ivfflat index would trade that for approximate
-- results, which is a bad trade when the whole point is to compare two methods
-- honestly.
-- ---------------------------------------------------------------------------

CREATE TABLE text_embeddings (
    owner_kind text NOT NULL CHECK (
        owner_kind IN ('claim_evidence', 'concept_claim', 'chunk', 'question')
    ),
    owner_key text NOT NULL CHECK (length(owner_key) BETWEEN 1 AND 200),
    model_id text NOT NULL REFERENCES embedding_models(model_id),
    -- The text that was embedded, by hash: a vector whose source cannot be
    -- identified is a number nobody can check.
    text_sha256 char(64) NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    embedding vector NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (owner_kind, owner_key, model_id)
);

CREATE INDEX text_embeddings_model_idx ON text_embeddings (model_id, owner_kind);

-- A comparison is a recorded run, not a printout somebody remembers. Both methods
-- answer the same question over the same statements, and the difference is the
-- result.
CREATE TABLE binding_method_comparisons (
    comparison_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ran_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ran_by text NOT NULL,
    statements integer NOT NULL CHECK (statements >= 0),
    -- {method: {topOne: n, overlapAtFive: n, ...}}
    summary jsonb NOT NULL,
    -- Per statement, what each method put first, so a person can look rather than
    -- take the summary on trust.
    detail jsonb NOT NULL,
    notes text
);

CREATE TRIGGER binding_method_comparisons_immutable
BEFORE UPDATE OR DELETE ON binding_method_comparisons
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

GRANT ALL ON text_embeddings, binding_method_comparisons TO radar_kx;
GRANT ALL ON embedding_models, chunk_embeddings TO radar_kx;

UPDATE metadata SET value = '20'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
