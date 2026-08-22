-- Read-only Radar KX extract for the corpus-membership contract (docs/radar-kb-corpus-membership-contract-2026-08-22.md).
--
-- Runs inside an explicitly read-only transaction: it cannot write to the production
-- evidence store even if invoked by a superuser. Emits one JSON document on stdout.
--
--   sudo -u postgres psql -d radar_kx -X -qAt -f corpus_membership_kx_extract.sql > kx-extract.json
--
-- The extract is the only KX input of corpus_membership_report.py, so the report can be
-- reproduced off-host from a stored extract without touching production again.

BEGIN READ ONLY;

SET LOCAL search_path = kx, public;

WITH complete_versions AS (
    SELECT document_id,
           count(*) AS complete_versions,
           max(length(canonical_text)) AS canonical_text_chars,
           string_agg(DISTINCT source_kind, ',' ORDER BY source_kind) AS source_kinds,
           string_agg(DISTINCT language, ',' ORDER BY language) AS languages
      FROM document_versions
     WHERE is_complete
     GROUP BY document_id
),
document_index AS (
    SELECT d.document_id,
           d.canonical_url,
           cv.document_id IS NOT NULL AS has_complete_version,
           EXISTS (SELECT 1 FROM material_documents md WHERE md.document_id = d.document_id)
               AS has_material
      FROM documents d
      LEFT JOIN complete_versions cv ON cv.document_id = d.document_id
),
perimeter_source_rollup AS (
    SELECT s.perimeter_source_id,
           s.source_kind,
           s.source_reference,
           s.source_sha256,
           s.captured_at,
           count(m.*) AS members,
           count(DISTINCT m.document_id) AS documents
      FROM issue_perimeter_sources s
      LEFT JOIN issue_perimeter_members m
             ON m.perimeter_source_id = s.perimeter_source_id
     GROUP BY s.perimeter_source_id, s.source_kind, s.source_reference,
              s.source_sha256, s.captured_at
)
SELECT json_build_object(
    'generatedAt', clock_timestamp(),
    'schemaVersion', (SELECT value FROM metadata WHERE key = 'schema_version'),
    'counts', json_build_object(
        'documents', (SELECT count(*) FROM documents),
        'documentsWithoutMaterial', (SELECT count(*) FROM document_index WHERE NOT has_material),
        'sourceMaterials', (SELECT count(*) FROM source_materials),
        'materialDocuments', (SELECT count(*) FROM material_documents),
        'materialDocumentsDistinctDocuments',
            (SELECT count(DISTINCT document_id) FROM material_documents),
        'documentVersions', (SELECT count(*) FROM document_versions),
        'documentVersionsComplete', (SELECT count(*) FROM document_versions WHERE is_complete),
        'documentsWithCompleteVersion', (SELECT count(*) FROM document_index WHERE has_complete_version),
        'chunks', (SELECT count(*) FROM chunks),
        'perimeterSources', (SELECT count(*) FROM issue_perimeter_sources),
        'perimeterMembers', (SELECT count(*) FROM issue_perimeter_members),
        'perimeterDocumentsUnion', (SELECT count(DISTINCT document_id) FROM issue_perimeter_members)
    ),
    'corpusImports', (
        SELECT coalesce(json_agg(json_build_object(
                   'corpusSha256', corpus_sha256,
                   'sourceName', source_name,
                   'rowCount', row_count,
                   'documentCount', document_count,
                   'importedAt', imported_at
               ) ORDER BY imported_at), '[]'::json)
          FROM corpus_imports
    ),
    'perimeterSources', (
        SELECT coalesce(json_agg(json_build_object(
                   'perimeterSourceId', perimeter_source_id,
                   'sourceKind', source_kind,
                   'sourceReference', source_reference,
                   'sourceSha256', source_sha256,
                   'capturedAt', captured_at,
                   'members', members,
                   'documents', documents
               ) ORDER BY captured_at), '[]'::json)
          FROM perimeter_source_rollup
    ),
    'perimeterMembers', (
        SELECT coalesce(json_agg(json_build_object(
                   'perimeterSourceId', perimeter_source_id,
                   'issueId', issue_id,
                   'issueDate', issue_date,
                   'materialRef', material_ref,
                   'documentId', document_id,
                   'canonicalUrl', canonical_url
               ) ORDER BY perimeter_source_id, issue_date, material_ref), '[]'::json)
          FROM issue_perimeter_members
    ),
    'perimeterDocuments', (
        SELECT coalesce(json_agg(json_build_object(
                   'documentId', di.document_id,
                   'canonicalUrl', di.canonical_url,
                   'hasCompleteVersion', di.has_complete_version,
                   'hasMaterial', di.has_material,
                   'completeVersions', coalesce(cv.complete_versions, 0),
                   'canonicalTextChars', coalesce(cv.canonical_text_chars, 0),
                   'sourceKinds', cv.source_kinds,
                   'languages', cv.languages
               ) ORDER BY di.document_id), '[]'::json)
          FROM document_index di
          LEFT JOIN complete_versions cv ON cv.document_id = di.document_id
         WHERE di.document_id IN (SELECT document_id FROM issue_perimeter_members)
    ),
    'documentIndex', (
        SELECT coalesce(json_agg(json_build_object(
                   'documentId', document_id,
                   'canonicalUrl', canonical_url,
                   'hasCompleteVersion', has_complete_version,
                   'hasMaterial', has_material
               ) ORDER BY document_id), '[]'::json)
          FROM document_index
    )
);

COMMIT;
