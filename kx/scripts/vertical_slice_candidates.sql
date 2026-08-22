-- Read-only extract of the issue-perimeter documents, with everything the
-- vertical-slice selection needs to be defensible (plan §13.1).
--
--   sudo -u postgres psql -d radar_kx -X -qAt -f vertical_slice_candidates.sql > candidates.json
--
-- Full text stays on Local Ru. What leaves is metadata plus a window signature:
-- sixteen evenly spaced 120-character windows, hashed. Two documents that share
-- several windows are near-certainly the same text, which is how the required
-- "pair of known reprints of one primary source" is found without copying a
-- single article off the host that stores it.

BEGIN READ ONLY;

SET LOCAL search_path = kx, public;

WITH current_source AS (
    SELECT perimeter_source_id
    FROM issue_perimeter_sources
    ORDER BY captured_at DESC
    LIMIT 1
),
members AS (
    SELECT m.document_id,
           count(*) AS selections,
           min(m.issue_date) AS first_issue_date,
           max(m.issue_date) AS last_issue_date,
           string_agg(DISTINCT m.perimeter, ',' ORDER BY m.perimeter) AS perimeters,
           string_agg(DISTINCT m.verdict, ',' ORDER BY m.verdict) AS verdicts,
           bool_or(m.key_material) AS key_material,
           string_agg(DISTINCT m.signal_strength, ',' ORDER BY m.signal_strength) AS signals,
           min(m.title) AS title
    FROM issue_perimeter_members AS m
    JOIN current_source USING (perimeter_source_id)
    GROUP BY m.document_id
),
best AS (
    -- The complete version a quotation would come from: newest wins, which is the
    -- same rule documents.best_version_id follows.
    SELECT DISTINCT ON (v.document_id)
           v.document_id, v.version_id, v.canonical_text, v.canonical_text_sha256,
           v.language, v.quality, v.source_kind, v.fetched_at
    FROM document_versions AS v
    JOIN members USING (document_id)
    WHERE v.is_complete
    ORDER BY v.document_id, v.fetched_at DESC, v.version_id
),
measured AS (
    SELECT best.*,
           length(best.canonical_text) AS chars,
           -- Numeric density: how much of the document is figures. The slice needs
           -- a document with numbers in it, and "mentions a year once" is not that.
           coalesce(
               array_length(
                   regexp_split_to_array(best.canonical_text, '[^0-9]+'), 1
               ) - 1, 0
           ) AS number_runs,
           (
               SELECT array_agg(
                          md5(substr(best.canonical_text,
                                     1 + (offsets.n * greatest(length(best.canonical_text) - 120, 0))
                                         / 16,
                                     120))
                          ORDER BY offsets.n)
               FROM generate_series(0, 15) AS offsets(n)
           ) AS window_signature
    FROM best
)
SELECT json_build_object(
    'generatedAt', clock_timestamp(),
    'perimeterSourceId', (SELECT perimeter_source_id FROM current_source),
    'documents', coalesce((
        SELECT json_agg(json_build_object(
                   'documentId', measured.document_id,
                   'versionId', measured.version_id,
                   'textSha256', measured.canonical_text_sha256,
                   'canonicalUrl', documents.canonical_url,
                   'host', lower(substring(documents.canonical_url FROM '^https?://([^/:?#]+)')),
                   'title', members.title,
                   'language', measured.language,
                   'quality', measured.quality,
                   'sourceKind', measured.source_kind,
                   'chars', measured.chars,
                   'numberRuns', measured.number_runs,
                   'chunks', (SELECT count(*) FROM chunks
                               WHERE chunks.version_id = measured.version_id),
                   'selections', members.selections,
                   'firstIssueDate', members.first_issue_date,
                   'lastIssueDate', members.last_issue_date,
                   'perimeters', members.perimeters,
                   'verdicts', members.verdicts,
                   'keyMaterial', members.key_material,
                   'signals', members.signals,
                   'accessMethod', provenance.source_access_method,
                   'archiveUsed', coalesce(provenance.archive_used, false),
                   'manualReviewRequired', coalesce(provenance.manual_review_required, false),
                   'windowSignature', measured.window_signature
               ) ORDER BY documents.canonical_url)
        FROM measured
        JOIN members USING (document_id)
        JOIN documents USING (document_id)
        LEFT JOIN version_provenance_current AS provenance
               ON provenance.version_id = measured.version_id
    ), '[]'::json)
);

COMMIT;
