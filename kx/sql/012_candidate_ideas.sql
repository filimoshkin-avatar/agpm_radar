BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Candidate ideas, and the independence verdict frozen into them (slice 2.9)
--
-- Owner decision P13: a candidate idea needs at least two supporting claims from
-- **different source families**, and below that it is not shown to the owner.
-- ADR-0007 §4 adds the part that shapes this migration: the independence verdict
-- is **stored with the assessment, not computed on read**. A rating that changes
-- because a family was edited afterwards is not a rating anybody can reason
-- about.
--
-- So an idea carries the numbers it was admitted on, and the version of the
-- family and cluster data those numbers came from. A correction next month
-- produces a new assessment; it does not silently rewrite what last month meant.
-- ---------------------------------------------------------------------------

ALTER TABLE ideas ADD COLUMN state text NOT NULL DEFAULT 'proposed'
    CHECK (state IN ('proposed', 'shown', 'accepted', 'rejected', 'superseded'));

-- How it was produced. NULL for an idea a person wrote.
ALTER TABLE ideas ADD COLUMN run_id uuid REFERENCES processing_runs(run_id);
ALTER TABLE ideas ADD COLUMN model text;
ALTER TABLE ideas ADD COLUMN prompt_sha256 char(64)
    CHECK (prompt_sha256 IS NULL OR prompt_sha256 ~ '^[0-9a-f]{64}$');

-- The independence verdict at the moment of production (ADR-0007 §4).
ALTER TABLE ideas ADD COLUMN independent_sources integer
    CHECK (independent_sources IS NULL OR independent_sources >= 0);
ALTER TABLE ideas ADD COLUMN unknown_documents integer
    CHECK (unknown_documents IS NULL OR unknown_documents >= 0);
ALTER TABLE ideas ADD COLUMN collapsed_by_family integer
    CHECK (collapsed_by_family IS NULL OR collapsed_by_family >= 0);
ALTER TABLE ideas ADD COLUMN collapsed_by_cluster integer
    CHECK (collapsed_by_cluster IS NULL OR collapsed_by_cluster >= 0);

-- Which family and cluster data the verdict was computed against. The high-water
-- decision id is the family layer's version; the confirmed-cluster count is the
-- duplicate layer's. Together they say "this is the world the idea was judged in".
ALTER TABLE ideas ADD COLUMN family_decision_high_water bigint;
ALTER TABLE ideas ADD COLUMN confirmed_cluster_count integer
    CHECK (confirmed_cluster_count IS NULL OR confirmed_cluster_count >= 0);

-- Did it clear P13's gate. Stored rather than derived, for the same reason.
ALTER TABLE ideas ADD COLUMN admitted boolean;

ALTER TABLE ideas ADD CONSTRAINT an_assessed_idea_carries_its_verdict CHECK (
    admitted IS NULL
    OR (
        independent_sources IS NOT NULL
        AND unknown_documents IS NOT NULL
        AND family_decision_high_water IS NOT NULL
        AND confirmed_cluster_count IS NOT NULL
    )
);

-- An idea that did not clear the gate is not shown to the owner (P13). Recording
-- it anyway is the point: "nothing was proposed this week" and "eleven things
-- were proposed and none had two independent sources" are different facts.
ALTER TABLE ideas ADD CONSTRAINT only_an_admitted_idea_is_shown CHECK (
    state = 'proposed' OR admitted IS TRUE
);

CREATE INDEX ideas_state_idx ON ideas (state, admitted);

-- Append-only. What the owner decided about an idea, and why.
CREATE TABLE idea_decisions (
    decision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idea_id uuid NOT NULL REFERENCES ideas(idea_id),
    verdict text NOT NULL CHECK (verdict IN ('accepted', 'rejected', 'superseded')),
    decided_by text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    rationale text NOT NULL CHECK (length(rationale) BETWEEN 1 AND 4000),
    superseded_by uuid REFERENCES ideas(idea_id),
    CONSTRAINT supersession_names_a_successor CHECK (
        (verdict = 'superseded') = (superseded_by IS NOT NULL)
    )
);

CREATE INDEX idea_decisions_idea_idx ON idea_decisions (idea_id, decision_id DESC);

CREATE TRIGGER idea_decisions_immutable
BEFORE UPDATE OR DELETE ON idea_decisions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

GRANT ALL ON idea_decisions TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE idea_decisions_decision_id_seq TO radar_kx;

UPDATE metadata SET value = '12'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
