BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Containment, because Jaccard could not see a press release
--
-- Slice 2.4 shipped with one similarity measure and was measured on the 275
-- documents of the perimeter the same day. The result: at a Jaccard threshold of
-- 0.80 it found no near-duplicate at all, and the perimeter looked clean.
--
-- It was not clean. Measuring containment - the shared shingles as a share of the
-- *shorter* text - found what the ADR was written for:
--
--   containment 1.000, Jaccard 0.593
--     finance.yahoo.com/.../planradar-ai-agents-close-gap
--     manilatimes.net/.../globenewswire/planradar-ai-agents-close-the-gap
--     One GlobeNewswire release, two outlets. The Yahoo text is wholly inside the
--     Manila Times page; Jaccard falls to 0.59 only because Manila Times wraps it
--     in 300 shingles of its own chrome.
--
--   containment 0.969, Jaccard 0.329
--     finance.yahoo.com/.../ai-agents ... and deloitte.com/.../press-room/...
--     A Deloitte press release carried almost verbatim. At Jaccard 0.33 this pair
--     is indistinguishable from two unrelated articles about one topic.
--
--   containment 0.773, Jaccard 0.588
--     pm.hse.ru and sovnet.ru announcing the AgPM Manifesto - a genuine
--     cross-host reprint where each side also adds text of its own.
--
-- Jaccard punishes a reprint for the boilerplate its host wraps around it, which
-- is precisely the shape syndication takes. Containment does not. Both are kept:
-- Jaccard still answers "are these two the same document", containment answers
-- "is one of these inside the other", and the cluster records which one fired.
-- ---------------------------------------------------------------------------

ALTER TABLE content_duplicate_clusters
    ADD COLUMN shingle_measure text CHECK (shingle_measure IN ('jaccard', 'containment'));

ALTER TABLE content_duplicate_clusters DROP CONSTRAINT shingle_clusters_state_their_rule;
ALTER TABLE content_duplicate_clusters ADD CONSTRAINT shingle_clusters_state_their_rule CHECK (
    (formation_method = 'shingle_overlap')
    = (shingle_threshold IS NOT NULL AND shingle_width IS NOT NULL AND shingle_measure IS NOT NULL)
);

ALTER TABLE duplicate_evidence DROP CONSTRAINT duplicate_evidence_evidence_kind_check;
ALTER TABLE duplicate_evidence ADD CONSTRAINT duplicate_evidence_evidence_kind_check CHECK (
    evidence_kind IN (
        'canonical_text_hash',
        'shingle_overlap',
        'shingle_containment',
        'shared_cited_primary_source'
    )
);

-- `similarity` holds the value of the measure named by `evidence_kind`; `detail`
-- carries both numbers, so a later review never has to recompute the one that
-- did not fire in order to understand why a cluster exists.
ALTER TABLE duplicate_evidence DROP CONSTRAINT shingle_evidence_states_its_overlap;
ALTER TABLE duplicate_evidence ADD CONSTRAINT shingle_evidence_states_its_overlap CHECK (
    evidence_kind NOT IN ('shingle_overlap', 'shingle_containment') OR similarity IS NOT NULL
);

UPDATE metadata SET value = '6'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
