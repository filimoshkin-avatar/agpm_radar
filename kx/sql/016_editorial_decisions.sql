BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Editorial decisions as events, and the queue an editor works (slice 2.12)
--
-- ADR-0006 §3: **every editorial decision is an append-only event with an actor**,
-- not a status overwritten in place. A status column that is updated cannot
-- answer "who decided this, and when did it change".
--
-- One table for all of them rather than a journal per object kind. A binding
-- confirmed, an idea accepted, an alias approved and a publication policy changed
-- are the same shape of fact - somebody decided something about something, under
-- some scope, for a reason - and one table means one place to read the history of
-- who has been deciding what.
--
-- The current state stays on the object, because a queue that has to replay a
-- journal to draw a list is a queue that gets drawn wrong. The journal is the
-- record; the column is the projection, and the code writes both in one
-- transaction.
-- ---------------------------------------------------------------------------

CREATE TABLE editorial_decisions (
    decision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_kind text NOT NULL CHECK (
        object_kind IN (
            'concept_evidence',
            'idea',
            'entity_alias',
            'source_family',
            'publication_policy',
            'knowledge_release'
        )
    ),
    -- Composite keys are serialised as "a/b". A text key rather than a uuid
    -- because the objects this covers are not all keyed the same way, and a
    -- journal that could only record half of them would be worse than none.
    object_key text NOT NULL CHECK (length(object_key) BETWEEN 1 AND 200),
    verdict text NOT NULL CHECK (verdict IN ('confirmed', 'rejected', 'deferred')),
    actor text NOT NULL CHECK (length(actor) > 0),
    -- Which scope the actor was using (ADR-0006 §12): who, what, when, with which
    -- scope, on which object.
    scope text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    rationale text
);

CREATE INDEX editorial_decisions_object_idx
    ON editorial_decisions (object_kind, object_key, decision_id DESC);
CREATE INDEX editorial_decisions_actor_idx ON editorial_decisions (actor, decided_at DESC);

CREATE TRIGGER editorial_decisions_immutable
BEFORE UPDATE OR DELETE ON editorial_decisions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- A rejection is a decision, not an absence. Without somewhere to record it, a
-- reviewer who looked at a proposal and said no leaves the same trace as one who
-- never opened it, and the queue offers it again tomorrow.
ALTER TABLE concept_evidence ADD COLUMN rejected_at timestamptz;
ALTER TABLE concept_evidence ADD COLUMN rejected_by text;

ALTER TABLE concept_evidence ADD CONSTRAINT rejection_is_whole CHECK (
    (rejected_at IS NULL) = (rejected_by IS NULL)
);
ALTER TABLE concept_evidence ADD CONSTRAINT a_binding_is_not_both CHECK (
    confirmed_at IS NULL OR rejected_at IS NULL
);

-- What the editor's queue reads. Ordered by relevance so the strongest proposal
-- for each statement is the one a reviewer sees first.
CREATE VIEW concept_evidence_queue AS
SELECT evidence.concept_claim_id,
       evidence.claim_id,
       evidence.relevance,
       evidence.membership_class,
       evidence.binding_method,
       claims.statement,
       claims.claim_nature,
       concepts.relative_path,
       versions.title AS concept_title,
       quote.quote_text,
       quote.char_start,
       quote.char_end,
       documents.canonical_url
FROM kx.concept_evidence AS evidence
JOIN kx.concept_claims AS claims USING (concept_claim_id)
JOIN kx.concept_versions AS versions USING (concept_version_id)
JOIN kx.concepts AS concepts USING (concept_id)
JOIN kx.claim_evidence AS quote ON quote.claim_id = evidence.claim_id
JOIN kx.document_versions AS source_versions
  ON source_versions.version_id = quote.version_id
JOIN kx.documents AS documents
  ON documents.document_id = source_versions.document_id
WHERE evidence.confirmed_at IS NULL
  AND evidence.rejected_at IS NULL
  AND quote.match_status = 'exact';

GRANT ALL ON editorial_decisions TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE editorial_decisions_decision_id_seq TO radar_kx;
GRANT SELECT ON concept_evidence_queue TO radar_kx;

UPDATE metadata SET value = '16'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
