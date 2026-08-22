BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- 1. Acquisition-step taxonomy
--
-- The escalation ladder has more rungs than the schema knew about, and one of
-- them - operator_artifact - was added straight to production as a hotfix on
-- 2026-08-22 without a migration (defect D1). Dropping the constraint by name
-- IF EXISTS and rebuilding it makes this migration idempotent against a
-- database that already carries the hand-applied ALTER, and puts the repository
-- back in charge of the schema.
--
-- The new kinds are the remaining rungs of the ladder: an ordinary HTTP request
-- carrying browser headers, a page that only yields text once rendered, a
-- snapshot taken from a web archive, and a file that was already on this host
-- and was never fetched at all. The last one exists because the AgPM canon is
-- loaded from local markdown, and calling that an operator artifact would be a
-- lie recorded in the evidence base.
-- ---------------------------------------------------------------------------

ALTER TABLE fetch_attempts DROP CONSTRAINT IF EXISTS fetch_attempts_source_kind_check;
ALTER TABLE fetch_attempts ADD CONSTRAINT fetch_attempts_source_kind_check CHECK (
    source_kind IN (
        'network',
        'network_robots_override',
        'network_browser_headers',
        'browser_render',
        'web_archive',
        'legacy_snapshot',
        'legacy_truncated',
        'operator_artifact',
        'local_import'
    )
);

ALTER TABLE document_versions DROP CONSTRAINT IF EXISTS document_versions_source_kind_check;
ALTER TABLE document_versions ADD CONSTRAINT document_versions_source_kind_check CHECK (
    source_kind IN (
        'network',
        'network_robots_override',
        'network_browser_headers',
        'browser_render',
        'web_archive',
        'legacy_snapshot',
        'legacy_truncated',
        'operator_artifact',
        'local_import'
    )
);

-- ---------------------------------------------------------------------------
-- 2. Version provenance, append-only
--
-- A version id is sha256 over (document, raw bytes, parser config, text), so
-- the way the bytes were obtained is not part of its identity and a wrong
-- source kind cannot be corrected by writing a new version - it would collide
-- on the primary key and on the UNIQUE constraint (defect D12). fetch_attempts
-- rows are immutable and stay exactly as they were recorded.
--
-- Provenance therefore lives beside the version, append-only. A correction is a
-- new row; the current provenance of a version is its latest row. Nothing is
-- ever overwritten, so "what did we believe about this version, and when" stays
-- answerable.
-- ---------------------------------------------------------------------------

CREATE TABLE version_provenance (
    provenance_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id char(64) NOT NULL REFERENCES document_versions(version_id),

    -- Which rung of the acquisition ladder actually produced these bytes.
    source_access_method text NOT NULL CHECK (
        source_access_method IN (
            'http_default',
            'browser_headers',
            'robots_override',
            'browser_render',
            'web_archive',
            'operator_file',
            'local_import'
        )
    ),

    -- Web archive. Rule 19 of the evidence contract requires a public quotation
    -- to point at the exact snapshot it came from, so an archive-derived version
    -- either carries its snapshot URL and capture date, or is explicitly marked
    -- for review - and stays unquotable until it is fixed.
    archive_used boolean NOT NULL DEFAULT false,
    archive_url text,
    archive_captured_at timestamptz,

    browser_used boolean NOT NULL DEFAULT false,

    -- Set when the provenance is known to be incomplete. Publication reads this.
    manual_review_required boolean NOT NULL DEFAULT false,
    manual_review_reason text,

    -- Operator-supplied and locally imported material: who handed it over, when,
    -- and what the material claims its own address is.
    provided_by text,
    provided_at timestamptz,
    original_url text,

    notes text,
    recorded_by text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),

    CONSTRAINT version_provenance_archive_method_is_marked CHECK (
        source_access_method <> 'web_archive' OR archive_used
    ),
    -- An archive snapshot without a URL and a capture date is not citable. It may
    -- exist - four documents are in exactly that state today - but it has to say so.
    CONSTRAINT version_provenance_archive_is_citable_or_flagged CHECK (
        NOT archive_used
        OR manual_review_required
        OR (archive_url IS NOT NULL AND archive_captured_at IS NOT NULL)
    ),
    CONSTRAINT version_provenance_archive_fields_need_archive CHECK (
        archive_used OR (archive_url IS NULL AND archive_captured_at IS NULL)
    ),
    CONSTRAINT version_provenance_render_uses_a_browser CHECK (
        source_access_method <> 'browser_render' OR browser_used
    ),
    -- Material that reached us by hand has to name a hand.
    CONSTRAINT version_provenance_handover_is_attributed CHECK (
        source_access_method NOT IN ('operator_file', 'local_import')
        OR (provided_by IS NOT NULL AND provided_at IS NOT NULL)
    ),
    CONSTRAINT version_provenance_review_states_a_reason CHECK (
        NOT manual_review_required OR manual_review_reason IS NOT NULL
    )
);

CREATE INDEX version_provenance_version_idx
    ON version_provenance (version_id, recorded_at DESC);

CREATE TRIGGER version_provenance_immutable
BEFORE UPDATE OR DELETE ON version_provenance
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- The latest provenance row per version: what we currently believe.
CREATE VIEW version_provenance_current AS
SELECT DISTINCT ON (version_id)
       version_id,
       provenance_id,
       source_access_method,
       archive_used,
       archive_url,
       archive_captured_at,
       browser_used,
       manual_review_required,
       manual_review_reason,
       provided_by,
       provided_at,
       original_url,
       notes,
       recorded_by,
       recorded_at
FROM kx.version_provenance
ORDER BY version_id, recorded_at DESC, provenance_id DESC;

-- Fail-closed input to publication: a version is quotable only when its
-- provenance is recorded and complete. Absent provenance blocks just as loudly
-- as bad provenance - the default is "no", not "probably fine".
CREATE VIEW version_publication_block AS
SELECT versions.version_id,
       versions.document_id,
       CASE
           WHEN current.version_id IS NULL THEN 'provenance_missing'
           WHEN current.manual_review_required THEN 'provenance_manual_review'
           WHEN current.archive_used
                AND (current.archive_url IS NULL OR current.archive_captured_at IS NULL)
               THEN 'archive_snapshot_unidentified'
       END AS block_reason
FROM kx.document_versions AS versions
LEFT JOIN kx.version_provenance_current AS current
       ON current.version_id = versions.version_id
WHERE current.version_id IS NULL
   OR current.manual_review_required
   OR (current.archive_used
       AND (current.archive_url IS NULL OR current.archive_captured_at IS NULL));

-- ---------------------------------------------------------------------------
-- 3. Per-source publication policy
--
-- The general rule is one paragraph with attribution and a link (owner decision
-- P32/P34) and it applies to everything. This table carries the attribution
-- wording a given source needs, and a shorter limit where one is warranted. A
-- row is an editorial decision, so it names who took it.
-- ---------------------------------------------------------------------------

CREATE TABLE source_publication_policy (
    source_key text PRIMARY KEY,
    source_kind text NOT NULL CHECK (
        source_kind IN ('host', 'corpus', 'source_family')
    ),
    attribution_template text NOT NULL,
    -- NULL means the general rule. A value may only tighten it.
    max_quote_chars integer CHECK (max_quote_chars IS NULL OR max_quote_chars > 0),
    requires_manual_review boolean NOT NULL DEFAULT false,
    notes text,
    decided_by text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- ---------------------------------------------------------------------------
-- 4. Egress audit (owner decision P18)
--
-- Full documents may leave Local Ru for exactly two approved model endpoints.
-- Every such call is recorded here: what was sent, how much of it, to whom, and
-- which processing run asked. The audit exists for us, independently of any
-- retention guarantee the provider does or does not give (P30).
-- ---------------------------------------------------------------------------

CREATE TABLE egress_audit (
    egress_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    provider text NOT NULL,
    model text NOT NULL,
    purpose text NOT NULL,
    run_id uuid REFERENCES processing_runs(run_id),
    document_id char(64) REFERENCES documents(document_id),
    version_id char(64) REFERENCES document_versions(version_id),
    chunk_id char(64) REFERENCES chunks(chunk_id),
    -- What actually crossed the boundary, by size and by hash. The text itself is
    -- not copied here: it is already in the store, and the hash proves which.
    payload_chars integer NOT NULL CHECK (payload_chars >= 0),
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    prompt_sha256 char(64) CHECK (prompt_sha256 IS NULL OR prompt_sha256 ~ '^[0-9a-f]{64}$'),
    request_tokens integer CHECK (request_tokens IS NULL OR request_tokens >= 0),
    response_tokens integer CHECK (response_tokens IS NULL OR response_tokens >= 0),
    outcome text NOT NULL,
    error_detail text,
    worker_release text NOT NULL
);

CREATE INDEX egress_audit_occurred_idx ON egress_audit (occurred_at DESC);
CREATE INDEX egress_audit_document_idx ON egress_audit (document_id, occurred_at DESC);

CREATE TRIGGER egress_audit_immutable
BEFORE UPDATE OR DELETE ON egress_audit
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- ---------------------------------------------------------------------------
-- 5. Wiki snapshots (owner decision P27)
--
-- The AgPM wiki is a directory of files that nothing versions. Every knowledge
-- release pins the state it was built from by snapshotting that directory here:
-- a manifest of per-file SHA-256 plus content-addressed blobs, the same idiom
-- raw_blobs uses. Copying the tree whole would cost tens of megabytes per
-- release; deduplication makes an unchanged file free and makes the difference
-- between two releases computable per page.
-- ---------------------------------------------------------------------------

CREATE TABLE wiki_blobs (
    blob_sha256 char(64) PRIMARY KEY CHECK (blob_sha256 ~ '^[0-9a-f]{64}$'),
    compression text NOT NULL CHECK (compression = 'gzip'),
    raw_bytes bigint NOT NULL CHECK (raw_bytes >= 0),
    stored_bytes bigint NOT NULL CHECK (stored_bytes >= 0),
    content bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE wiki_snapshots (
    snapshot_id text PRIMARY KEY,
    taken_at timestamptz NOT NULL,
    -- SHA-256 over the sorted "path sha256" manifest: one value that changes when
    -- anything in the perimeter changes, and the identifier a knowledge release
    -- points at.
    manifest_sha256 char(64) NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    perimeter text NOT NULL,
    file_count integer NOT NULL CHECK (file_count >= 0),
    total_bytes bigint NOT NULL CHECK (total_bytes >= 0),
    notes text,
    recorded_by text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE wiki_snapshot_files (
    snapshot_id text NOT NULL REFERENCES wiki_snapshots(snapshot_id),
    relative_path text NOT NULL,
    blob_sha256 char(64) NOT NULL REFERENCES wiki_blobs(blob_sha256),
    bytes bigint NOT NULL CHECK (bytes >= 0),
    PRIMARY KEY (snapshot_id, relative_path)
);

CREATE INDEX wiki_snapshot_files_blob_idx ON wiki_snapshot_files (blob_sha256);

CREATE TRIGGER wiki_blobs_immutable
BEFORE UPDATE OR DELETE ON wiki_blobs
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER wiki_snapshots_immutable
BEFORE UPDATE OR DELETE ON wiki_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER wiki_snapshot_files_immutable
BEFORE UPDATE OR DELETE ON wiki_snapshot_files
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- ---------------------------------------------------------------------------
-- 6. Store reconciliation reports (owner decision P28)
--
-- The file store is the Project Manager's working copy and KX is the evidence
-- base for publication. They will drift - that is accepted. What is not
-- acceptable is finding out at publication time, so the comparison is recorded
-- on a schedule and the divergence is a number somebody can look at.
-- ---------------------------------------------------------------------------

CREATE TABLE store_reconciliation_reports (
    report_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    generated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    scope text NOT NULL,
    file_store_count integer NOT NULL CHECK (file_store_count >= 0),
    kx_count integer NOT NULL CHECK (kx_count >= 0),
    only_in_file_store integer NOT NULL CHECK (only_in_file_store >= 0),
    only_in_kx integer NOT NULL CHECK (only_in_kx >= 0),
    differing integer NOT NULL CHECK (differing >= 0),
    payload jsonb NOT NULL,
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    generated_by text NOT NULL
);

CREATE INDEX store_reconciliation_reports_scope_idx
    ON store_reconciliation_reports (scope, generated_at DESC);

CREATE TRIGGER store_reconciliation_reports_immutable
BEFORE UPDATE OR DELETE ON store_reconciliation_reports
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- ---------------------------------------------------------------------------
-- 7. Corpus membership class (corpus-membership contract §9)
--
-- The AgPM canon and the external standards are loaded as their own corpus.
-- They are not Radar materials, they never enter the issue perimeter, and they
-- must never land in a coverage denominator computed over materials.
-- ---------------------------------------------------------------------------

ALTER TABLE corpus_imports
    ADD COLUMN source_kind text NOT NULL DEFAULT 'radar_materials';

ALTER TABLE corpus_imports
    ADD CONSTRAINT corpus_imports_source_kind_check CHECK (
        source_kind IN ('radar_materials', 'canon_import', 'operator_import')
    );

-- A canon document has no web address, and document_id is sha256 over the
-- canonical URL, so it needs one anyway. The reserved scheme gives it a stable
-- identity that cannot collide with a fetched page and cannot be mistaken for
-- one. Reserving it here rather than in code means a third scheme cannot appear
-- by accident: all 8313 documents at 2026-08-22 are http(s), so the constraint
-- validates without touching a row.
ALTER TABLE documents
    ADD CONSTRAINT documents_canonical_url_scheme CHECK (
        canonical_url ~ '^https?://' OR canonical_url ~ '^agpm-canon:/[^/]'
    );

-- ---------------------------------------------------------------------------
-- 8. Grants
--
-- 001 granted the service role out of band, so every object added later grants
-- explicitly or the deployed worker cannot read or write it.
-- ---------------------------------------------------------------------------

GRANT ALL ON version_provenance, source_publication_policy, egress_audit,
             wiki_blobs, wiki_snapshots, wiki_snapshot_files,
             store_reconciliation_reports TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE version_provenance_provenance_id_seq TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE egress_audit_egress_id_seq TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE store_reconciliation_reports_report_id_seq TO radar_kx;
GRANT SELECT ON version_provenance_current, version_publication_block TO radar_kx;

UPDATE metadata SET value = '3'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
