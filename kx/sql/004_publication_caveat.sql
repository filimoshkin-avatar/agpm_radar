BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Withheld versus published-with-a-caveat
--
-- Migration 003 treated every flagged provenance the same way: nothing could be
-- quoted. The owner decided on 2026-08-22 that the two cases are not the same
-- (ADR-0004, rules 21а and 21б).
--
-- Text from a web archive whose snapshot URL and date were not recorded is a
-- real quotation of a real source. What is missing is the reader's ability to
-- re-check it at that exact snapshot. That is a statement about the link, and a
-- visible caveat says it honestly. Four documents are in that state.
--
-- Our own excerpt or note about a source is a different thing entirely: the
-- words are ours. Attributing them to the source would be a false quotation, and
-- no caveat repairs a false attribution. Absent provenance blocks for the same
-- reason - we cannot say where the words came from at all.
--
-- Both are derived from provenance that already exists, so this migration adds
-- no column: it splits one view into two that say what they mean.
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS version_publication_block;

-- Quotation refused outright.
CREATE VIEW version_publication_block AS
SELECT versions.version_id,
       versions.document_id,
       CASE
           WHEN current.version_id IS NULL THEN 'provenance_missing'
           ELSE 'provenance_manual_review'
       END AS block_reason
FROM kx.document_versions AS versions
LEFT JOIN kx.version_provenance_current AS current
       ON current.version_id = versions.version_id
WHERE current.version_id IS NULL
   OR (
        current.manual_review_required
        -- the archive-without-a-snapshot case is a caveat, not a refusal
        AND NOT (
            current.archive_used
            AND (current.archive_url IS NULL OR current.archive_captured_at IS NULL)
        )
      );

-- Quotation allowed, and the reader is told what cannot be re-checked.
CREATE VIEW version_publication_caveat AS
SELECT current.version_id,
       versions.document_id,
       'archive_snapshot_not_recorded' AS caveat,
       coalesce(
           current.manual_review_reason,
           'text came from a web archive; the snapshot was not preserved'
       ) AS caveat_detail
FROM kx.version_provenance_current AS current
JOIN kx.document_versions AS versions USING (version_id)
WHERE current.archive_used
  AND (current.archive_url IS NULL OR current.archive_captured_at IS NULL);

GRANT SELECT ON version_publication_block, version_publication_caveat TO radar_kx;

UPDATE metadata SET value = '4'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
