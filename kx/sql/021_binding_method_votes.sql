BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Which linking method was right, decided by looking (owner request)
--
-- The first comparison put the two methods this far apart: over 233 statements
-- they chose the same best evidence **4 times**, their top-five sets shared on
-- average 0.116 items, and for 206 of the 233 they had nothing in common at all.
--
-- Two methods that disagree almost completely cannot both be judged by their own
-- scores, and neither score means what it looks like: e5 gives 0.89 to a good
-- match and 0.86 to nonsense, and reciprocal rank fusion saturates at 2/61. The
-- only instrument left is a person looking at the pair.
--
-- A vote is not an editorial decision about the corpus, so it does not go in
-- `editorial_decisions`: nothing about the knowledge base changes because of it.
-- It is evidence about the tooling, and it gets its own table.
-- ---------------------------------------------------------------------------

CREATE TABLE binding_method_votes (
    vote_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    concept_claim_id uuid NOT NULL REFERENCES concept_claims(concept_claim_id),
    -- Which candidate the person preferred, and the ids of both, so the vote can
    -- be replayed against a later run of either method.
    winner text NOT NULL CHECK (winner IN ('lexical', 'semantic', 'neither', 'both')),
    lexical_claim_id uuid REFERENCES claims(claim_id),
    semantic_claim_id uuid REFERENCES claims(claim_id),
    voted_by text NOT NULL,
    voted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (concept_claim_id, voted_by)
);

CREATE INDEX binding_method_votes_winner_idx ON binding_method_votes (winner);

CREATE TRIGGER binding_method_votes_immutable
BEFORE UPDATE OR DELETE ON binding_method_votes
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

GRANT ALL ON binding_method_votes TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE binding_method_votes_vote_id_seq TO radar_kx;

UPDATE metadata SET value = '21'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
