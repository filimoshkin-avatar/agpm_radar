BEGIN;

SET search_path = kx, public;

-- Audited snapshot of one editorial source that selected materials into Radar issues.
CREATE TABLE issue_perimeter_sources (
    perimeter_source_id text PRIMARY KEY,
    source_kind text NOT NULL CHECK (
        source_kind IN ('v2_content_release', 'legacy_radar_db')
    ),
    source_reference text NOT NULL,
    source_sha256 char(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    captured_at timestamptz NOT NULL,
    row_count integer NOT NULL CHECK (row_count >= 0),
    document_count integer NOT NULL CHECK (document_count >= 0),
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- One issue/material selection, linked to the canonical KX document it points at.
CREATE TABLE issue_perimeter_members (
    perimeter_source_id text NOT NULL
        REFERENCES issue_perimeter_sources(perimeter_source_id),
    issue_id text NOT NULL,
    material_ref text NOT NULL,
    document_id char(64) NOT NULL REFERENCES documents(document_id),
    issue_date date NOT NULL,
    issue_number integer,
    issue_title text NOT NULL DEFAULT '',
    sort_order integer NOT NULL CHECK (sort_order >= 0),
    perimeter text NOT NULL CHECK (perimeter IN ('near', 'mid', 'far')),
    verdict text CHECK (verdict IS NULL OR verdict IN ('core', 'adjacent')),
    key_material boolean NOT NULL DEFAULT false,
    signal_score integer,
    signal_strength text,
    title text NOT NULL DEFAULT '',
    source_url text NOT NULL,
    canonical_url text NOT NULL,
    summary text,
    agpm_takeaway text,
    brief text,
    trend_notes text,
    theses jsonb NOT NULL DEFAULT '[]'::jsonb,
    flags jsonb NOT NULL DEFAULT '{}'::jsonb,
    published_raw text,
    payload jsonb NOT NULL,
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (perimeter_source_id, issue_id, material_ref)
);

CREATE INDEX issue_perimeter_members_document_idx
    ON issue_perimeter_members (document_id);
CREATE INDEX issue_perimeter_members_issue_idx
    ON issue_perimeter_members (issue_date, issue_id, sort_order);

CREATE TRIGGER issue_perimeter_sources_immutable
BEFORE UPDATE OR DELETE ON issue_perimeter_sources
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER issue_perimeter_members_immutable
BEFORE UPDATE OR DELETE ON issue_perimeter_members
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- One row per distinct document that any Radar issue ever selected.
CREATE VIEW issue_perimeter_documents AS
SELECT documents.document_id,
       documents.canonical_url,
       documents.best_version_id,
       count(*) AS member_rows,
       count(DISTINCT members.issue_id) AS issue_count,
       count(DISTINCT members.perimeter_source_id) AS source_count,
       min(members.issue_date) AS first_issue_date,
       max(members.issue_date) AS last_issue_date,
       bool_or(members.key_material) AS key_material
FROM kx.issue_perimeter_members AS members
JOIN kx.documents AS documents USING (document_id)
GROUP BY documents.document_id, documents.canonical_url, documents.best_version_id;

-- Per-document fetch controls. Both default to the global policy; a non-default
-- value is an explicit, reasoned, auditable decision for one document.
ALTER TABLE fetch_queue
    ADD COLUMN robots_override boolean NOT NULL DEFAULT false,
    ADD COLUMN robots_override_reason text,
    ADD COLUMN body_limit_bytes bigint
        CHECK (body_limit_bytes IS NULL OR body_limit_bytes > 0);

ALTER TABLE fetch_queue
    ADD CONSTRAINT fetch_queue_override_requires_reason
    CHECK (NOT robots_override OR robots_override_reason IS NOT NULL);

-- A fetch performed under an explicit robots override keeps its own source kind,
-- so overridden evidence can never be mistaken for ordinary robots-respecting evidence.
ALTER TABLE fetch_attempts DROP CONSTRAINT fetch_attempts_source_kind_check;
ALTER TABLE fetch_attempts ADD CONSTRAINT fetch_attempts_source_kind_check CHECK (
    source_kind IN (
        'network', 'network_robots_override', 'legacy_snapshot', 'legacy_truncated'
    )
);

ALTER TABLE document_versions DROP CONSTRAINT document_versions_source_kind_check;
ALTER TABLE document_versions ADD CONSTRAINT document_versions_source_kind_check CHECK (
    source_kind IN (
        'network', 'network_robots_override', 'legacy_snapshot', 'legacy_truncated'
    )
);

-- Derived re-parse of already retained raw evidence. No network request is made,
-- so it is recorded here instead of in the immutable HTTP attempt log.
CREATE TABLE reparse_runs (
    reparse_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id char(64) NOT NULL REFERENCES documents(document_id),
    raw_sha256 char(64) NOT NULL REFERENCES raw_blobs(raw_sha256),
    version_id char(64) REFERENCES document_versions(version_id),
    parser_name text NOT NULL,
    parser_version text NOT NULL,
    parser_config_sha256 char(64) NOT NULL CHECK (
        parser_config_sha256 ~ '^[0-9a-f]{64}$'
    ),
    reason text NOT NULL,
    outcome text NOT NULL,
    worker_release text NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL
);

CREATE INDEX reparse_runs_document_idx ON reparse_runs (document_id, finished_at DESC);

CREATE TRIGGER reparse_runs_immutable
BEFORE UPDATE OR DELETE ON reparse_runs
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- The service role reads and writes; DDL stays with the migration role. 001 granted
-- this out of band, so every object added after it must grant explicitly.
GRANT ALL ON issue_perimeter_sources, issue_perimeter_members, reparse_runs TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE reparse_runs_reparse_id_seq TO radar_kx;
GRANT SELECT ON issue_perimeter_documents TO radar_kx;

UPDATE metadata SET value = '2'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
