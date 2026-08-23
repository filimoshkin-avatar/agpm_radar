BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- "Looked at, no relation" is an answer and has to be recorded (stage 2)
--
-- The judge answers `none` for most pairs - deliberately, because the shortlist
-- is wide. But `none` wrote nothing, so `link_candidates` offered the same pair
-- again on the next run, and the second run's answer went in beside the first
-- one's silence.
--
-- Measured on the production pass: 21 910 pairs judged, 13 671 linked, 8 239 left
-- alone. Re-running re-judged all 8 239 - and added links to some of them, which
-- is the part that matters. The judge is not deterministic, so a base that
-- re-judges every unlinked pair forever drifts monotonically toward "everything
-- is related to everything", one re-run at a time. Nothing about that drift is
-- visible in the link table: it looks like the base learning more.
--
-- So this records the negative. A pair judged once is not offered again unless
-- somebody deliberately clears the row, and the count of what was looked at and
-- left alone becomes a number rather than the absence of one.
-- ---------------------------------------------------------------------------

CREATE TABLE link_judgements (
    from_id uuid NOT NULL REFERENCES claims(claim_id),
    to_id uuid NOT NULL REFERENCES claims(claim_id),
    -- What the judge said. `none` is the only value that lands here: everything
    -- else is a row in `knowledge_links` and this table would duplicate it.
    verdict text NOT NULL DEFAULT 'none' CHECK (verdict = 'none'),
    -- Kept so a later, better judge can be told apart from this one, and so a
    -- re-judgement can be scoped to what an older model left alone.
    judged_by text NOT NULL,
    judged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (from_id, to_id),
    CONSTRAINT a_pair_is_not_itself CHECK (from_id <> to_id)
);

CREATE INDEX link_judgements_judge_idx ON link_judgements (judged_by, judged_at DESC);

GRANT ALL ON link_judgements TO radar_kx;

UPDATE metadata SET value = '25'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
