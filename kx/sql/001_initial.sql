BEGIN;

CREATE SCHEMA IF NOT EXISTS kx AUTHORIZATION radar_kx;
SET search_path = kx, public;

CREATE TABLE metadata (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO metadata (key, value)
VALUES ('schema_version', '1'::jsonb)
ON CONFLICT (key) DO NOTHING;

CREATE TABLE corpus_imports (
    corpus_sha256 char(64) PRIMARY KEY CHECK (corpus_sha256 ~ '^[0-9a-f]{64}$'),
    source_name text NOT NULL,
    row_count integer NOT NULL CHECK (row_count >= 0),
    document_count integer NOT NULL CHECK (document_count >= 0),
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE documents (
    document_id char(64) PRIMARY KEY CHECK (document_id ~ '^[0-9a-f]{64}$'),
    canonical_url text NOT NULL UNIQUE,
    first_seen_at timestamptz,
    last_seen_at timestamptz,
    best_version_id char(64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE source_materials (
    material_id text PRIMARY KEY,
    source_url text NOT NULL,
    canonical_url text NOT NULL,
    title text NOT NULL DEFAULT '',
    summary text NOT NULL DEFAULT '',
    raw_excerpt text NOT NULL DEFAULT '',
    perimeter text,
    published_raw text,
    first_seen_at timestamptz,
    last_seen_at timestamptz,
    payload jsonb NOT NULL,
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    corpus_sha256 char(64) NOT NULL REFERENCES corpus_imports(corpus_sha256),
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE source_material_revisions (
    material_id text NOT NULL,
    corpus_sha256 char(64) NOT NULL REFERENCES corpus_imports(corpus_sha256),
    document_id char(64) NOT NULL REFERENCES documents(document_id),
    source_url text NOT NULL,
    canonical_url text NOT NULL,
    payload jsonb NOT NULL,
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (material_id, corpus_sha256)
);

CREATE INDEX source_material_revisions_document_idx
    ON source_material_revisions (document_id, imported_at);

CREATE TABLE material_documents (
    material_id text PRIMARY KEY REFERENCES source_materials(material_id),
    document_id char(64) NOT NULL REFERENCES documents(document_id)
);

CREATE INDEX material_documents_document_idx ON material_documents (document_id);

CREATE TABLE raw_blobs (
    raw_sha256 char(64) PRIMARY KEY CHECK (raw_sha256 ~ '^[0-9a-f]{64}$'),
    compression text NOT NULL CHECK (compression = 'gzip'),
    raw_bytes bigint NOT NULL CHECK (raw_bytes >= 0),
    stored_bytes bigint NOT NULL CHECK (stored_bytes >= 0),
    content bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE fetch_attempts (
    attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_group uuid NOT NULL DEFAULT gen_random_uuid(),
    document_id char(64) NOT NULL REFERENCES documents(document_id),
    source_kind text NOT NULL CHECK (
        source_kind IN ('network', 'legacy_snapshot', 'legacy_truncated')
    ),
    requested_url text NOT NULL,
    final_url text,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    http_status integer,
    content_type text,
    response_headers jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_sha256 char(64) REFERENCES raw_blobs(raw_sha256),
    outcome text NOT NULL,
    error_detail text,
    worker_release text NOT NULL
);

CREATE INDEX fetch_attempts_document_time_idx
    ON fetch_attempts (document_id, finished_at DESC);
CREATE INDEX fetch_attempts_outcome_idx ON fetch_attempts (outcome);
CREATE UNIQUE INDEX fetch_attempts_legacy_dedupe_idx
    ON fetch_attempts (document_id, source_kind, raw_sha256)
    WHERE source_kind IN ('legacy_snapshot', 'legacy_truncated')
      AND raw_sha256 IS NOT NULL;

CREATE TABLE document_versions (
    version_id char(64) PRIMARY KEY CHECK (version_id ~ '^[0-9a-f]{64}$'),
    document_id char(64) NOT NULL REFERENCES documents(document_id),
    raw_sha256 char(64) NOT NULL REFERENCES raw_blobs(raw_sha256),
    source_kind text NOT NULL CHECK (
        source_kind IN ('network', 'legacy_snapshot', 'legacy_truncated')
    ),
    canonical_text text NOT NULL,
    canonical_text_sha256 char(64) NOT NULL CHECK (
        canonical_text_sha256 ~ '^[0-9a-f]{64}$'
    ),
    title text NOT NULL DEFAULT '',
    language text NOT NULL CHECK (language IN ('ru', 'en', 'mixed', 'und')),
    parser_name text NOT NULL,
    parser_version text NOT NULL,
    parser_config_sha256 char(64) NOT NULL CHECK (
        parser_config_sha256 ~ '^[0-9a-f]{64}$'
    ),
    quality text NOT NULL,
    is_complete boolean NOT NULL,
    fetched_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (document_id, raw_sha256, parser_config_sha256, canonical_text_sha256)
);

ALTER TABLE documents
    ADD CONSTRAINT documents_best_version_fk
    FOREIGN KEY (best_version_id) REFERENCES document_versions(version_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX document_versions_document_time_idx
    ON document_versions (document_id, fetched_at DESC);
CREATE INDEX document_versions_title_trgm_idx
    ON document_versions USING gin (title gin_trgm_ops);

CREATE TABLE fetch_queue (
    document_id char(64) PRIMARY KEY REFERENCES documents(document_id),
    status text NOT NULL CHECK (
        status IN ('pending', 'running', 'retry', 'succeeded', 'failed', 'skipped')
    ),
    priority integer NOT NULL DEFAULT 0,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_token uuid,
    lease_until timestamptz,
    last_http_status integer,
    last_error_code text,
    last_error_detail text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX fetch_queue_ready_idx
    ON fetch_queue (status, next_attempt_at, priority DESC, created_at)
    WHERE status IN ('pending', 'retry');

CREATE TABLE processing_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id char(64) NOT NULL REFERENCES document_versions(version_id),
    processor text NOT NULL,
    processor_version text NOT NULL,
    parameters_sha256 char(64) NOT NULL CHECK (parameters_sha256 ~ '^[0-9a-f]{64}$'),
    model_id text,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    raw_output jsonb,
    input_tokens bigint,
    output_tokens bigint,
    cost_microusd bigint,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    UNIQUE (version_id, processor, processor_version, parameters_sha256, model_id)
);

CREATE TABLE chunks (
    chunk_id char(64) PRIMARY KEY CHECK (chunk_id ~ '^[0-9a-f]{64}$'),
    version_id char(64) NOT NULL REFERENCES document_versions(version_id),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end > char_start),
    text text NOT NULL,
    text_sha256 char(64) NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    search_ru tsvector GENERATED ALWAYS AS (
        to_tsvector('pg_catalog.russian'::regconfig, text)
    ) STORED,
    search_en tsvector GENERATED ALWAYS AS (
        to_tsvector('pg_catalog.english'::regconfig, text)
    ) STORED,
    UNIQUE (version_id, ordinal),
    UNIQUE (version_id, char_start, char_end)
);

CREATE INDEX chunks_version_idx ON chunks (version_id, ordinal);
CREATE INDEX chunks_search_ru_idx ON chunks USING gin (search_ru);
CREATE INDEX chunks_search_en_idx ON chunks USING gin (search_en);

CREATE TABLE embedding_models (
    model_id text PRIMARY KEY,
    dimensions integer NOT NULL CHECK (dimensions > 0),
    provider text NOT NULL,
    parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE chunk_embeddings (
    chunk_id char(64) NOT NULL REFERENCES chunks(chunk_id),
    model_id text NOT NULL REFERENCES embedding_models(model_id),
    embedding vector NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (chunk_id, model_id)
);

CREATE TABLE entities (
    entity_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type text NOT NULL,
    canonical_name text NOT NULL,
    description text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (entity_type, canonical_name)
);

CREATE TABLE entity_aliases (
    entity_id uuid NOT NULL REFERENCES entities(entity_id),
    alias text NOT NULL,
    language text NOT NULL DEFAULT 'und',
    origin text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (entity_id, alias, language)
);

CREATE TABLE entity_merges (
    merge_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    from_entity_id uuid NOT NULL REFERENCES entities(entity_id),
    to_entity_id uuid NOT NULL REFERENCES entities(entity_id),
    rationale text NOT NULL,
    actor text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reverted_at timestamptz,
    CHECK (from_entity_id <> to_entity_id)
);

CREATE TABLE claims (
    claim_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id char(64) NOT NULL REFERENCES document_versions(version_id),
    processing_run_id uuid NOT NULL REFERENCES processing_runs(run_id),
    claim_kind text NOT NULL CHECK (claim_kind IN ('asserted', 'derived', 'inferred')),
    subject_entity_id uuid REFERENCES entities(entity_id),
    predicate text NOT NULL,
    object_text text NOT NULL,
    normalized_text text NOT NULL,
    confidence numeric(6,5),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE claim_evidence (
    claim_id uuid NOT NULL REFERENCES claims(claim_id),
    version_id char(64) NOT NULL REFERENCES document_versions(version_id),
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end > char_start),
    quote_text text NOT NULL,
    quote_sha256 char(64) NOT NULL CHECK (quote_sha256 ~ '^[0-9a-f]{64}$'),
    match_status text NOT NULL CHECK (match_status IN ('exact', 'rejected')),
    PRIMARY KEY (claim_id, version_id, char_start, char_end)
);

CREATE FUNCTION validate_exact_claim_evidence() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    version_text text;
    claim_version char(64);
    calculated_quote_hash text;
BEGIN
    SELECT canonical_text INTO STRICT version_text
    FROM document_versions WHERE version_id = NEW.version_id;

    SELECT version_id INTO STRICT claim_version
    FROM claims WHERE claim_id = NEW.claim_id;

    IF claim_version <> NEW.version_id THEN
        RAISE EXCEPTION 'claim and evidence version mismatch';
    END IF;

    calculated_quote_hash := encode(digest(convert_to(NEW.quote_text, 'UTF8'), 'sha256'), 'hex');
    IF calculated_quote_hash <> NEW.quote_sha256 THEN
        RAISE EXCEPTION 'claim evidence quote hash mismatch';
    END IF;

    IF NEW.match_status = 'exact'
       AND substr(version_text, NEW.char_start + 1, NEW.char_end - NEW.char_start)
           <> NEW.quote_text THEN
        RAISE EXCEPTION 'claim evidence is not an exact canonical-text span';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER claim_evidence_exact_span
BEFORE INSERT OR UPDATE ON claim_evidence
FOR EACH ROW EXECUTE FUNCTION validate_exact_claim_evidence();

CREATE TABLE metrics (
    metric_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id uuid NOT NULL REFERENCES claims(claim_id),
    value_numeric numeric NOT NULL,
    unit text NOT NULL,
    currency text,
    period_start date,
    period_end date,
    as_of_date date,
    subject text,
    method text
);

CREATE TABLE relations (
    relation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    src_entity_id uuid NOT NULL REFERENCES entities(entity_id),
    dst_entity_id uuid NOT NULL REFERENCES entities(entity_id),
    relation_type text NOT NULL,
    claim_id uuid NOT NULL REFERENCES claims(claim_id),
    valid_from timestamptz,
    valid_to timestamptz,
    CHECK (src_entity_id <> dst_entity_id)
);

CREATE TABLE ideas (
    idea_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title text NOT NULL,
    statement text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE idea_evidence (
    idea_id uuid NOT NULL REFERENCES ideas(idea_id),
    claim_id uuid NOT NULL REFERENCES claims(claim_id),
    stance text NOT NULL CHECK (stance IN ('support', 'contradict', 'context')),
    PRIMARY KEY (idea_id, claim_id)
);

CREATE TABLE idea_scores (
    idea_id uuid NOT NULL REFERENCES ideas(idea_id),
    formula_version text NOT NULL,
    features jsonb NOT NULL,
    score numeric NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (idea_id, formula_version, computed_at)
);

CREATE FUNCTION reject_immutable_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable table %.% does not allow %',
        TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP;
END;
$$;

CREATE TRIGGER raw_blobs_immutable
BEFORE UPDATE OR DELETE ON raw_blobs
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER fetch_attempts_immutable
BEFORE UPDATE OR DELETE ON fetch_attempts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER document_versions_immutable
BEFORE UPDATE OR DELETE ON document_versions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER source_material_revisions_immutable
BEFORE UPDATE OR DELETE ON source_material_revisions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

COMMIT;
