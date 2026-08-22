BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Extraction: exact evidence and everything else, kept apart (plan §10.2)
--
-- The design rule this migration exists to enforce: `claim_evidence` stays
-- exact-only. A model returns a verbatim quotation, this side finds the offsets
-- deterministically, and only a span that reproduces itself out of the stored
-- text becomes evidence. Everything the model said that could not be pinned to
-- the store lands in `extraction_candidates` with the reason it could not, and
-- moves into `claim_evidence` only through an explicit validation that produces
-- an exact span.
--
-- The alternative that was rejected - a `candidate` state inside
-- `claim_evidence` - would have put unverified text one WHERE clause away from
-- being cited.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- 1. A claim has a verification state, and the verdicts are a journal
-- ---------------------------------------------------------------------------

ALTER TABLE claims ADD COLUMN state text NOT NULL DEFAULT 'proposed'
    CHECK (state IN ('proposed', 'accepted', 'rejected', 'superseded'));

CREATE INDEX claims_state_idx ON claims (state);

-- Append-only. A claim that was accepted in March and rejected in May has two
-- rows, and the March rating can still say what it was based on.
CREATE TABLE claim_verdicts (
    verdict_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id uuid NOT NULL REFERENCES claims(claim_id),
    verdict text NOT NULL CHECK (verdict IN ('accepted', 'rejected', 'superseded')),
    decided_by text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    rationale text NOT NULL CHECK (length(rationale) BETWEEN 1 AND 4000),
    -- Set when the verdict is 'superseded': which claim replaced this one.
    superseded_by uuid REFERENCES claims(claim_id),
    CONSTRAINT supersession_names_a_successor CHECK (
        (verdict = 'superseded') = (superseded_by IS NOT NULL)
    ),
    CONSTRAINT a_claim_cannot_supersede_itself CHECK (
        superseded_by IS NULL OR superseded_by <> claim_id
    )
);

CREATE INDEX claim_verdicts_claim_idx ON claim_verdicts (claim_id, verdict_id DESC);

CREATE TRIGGER claim_verdicts_immutable
BEFORE UPDATE OR DELETE ON claim_verdicts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- ---------------------------------------------------------------------------
-- 2. Candidates: what the model said that the store could not confirm
-- ---------------------------------------------------------------------------

CREATE TABLE extraction_candidates (
    candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id char(64) NOT NULL REFERENCES document_versions(version_id),
    -- The chunk the fragment was taken from, when a run works chunk by chunk.
    chunk_id char(64) REFERENCES chunks(chunk_id),
    run_id uuid NOT NULL REFERENCES processing_runs(run_id),

    -- What was proposed.
    predicate text NOT NULL,
    object_text text NOT NULL,
    proposed_quote text NOT NULL,
    proposed_quote_sha256 char(64) NOT NULL
        CHECK (proposed_quote_sha256 ~ '^[0-9a-f]{64}$'),

    -- Why it is not evidence. `numeric_disagreement` is the blocking category of
    -- plan §11.3: a number, date or unit that differs inside a span both models
    -- otherwise confirmed is an error to look at, not a claim to downgrade.
    reason text NOT NULL CHECK (
        reason IN (
            'quote_not_found',
            'quote_ambiguous',
            'quote_outside_offered_window',
            'quote_too_short',
            'cross_check_failed',
            'numeric_disagreement',
            'model_refused',
            'malformed_output'
        )
    ),
    reason_detail text,

    -- How it was produced, so a candidate can be re-judged when the recipe changes.
    model text NOT NULL,
    prompt_sha256 char(64) NOT NULL CHECK (prompt_sha256 ~ '^[0-9a-f]{64}$'),
    extractor_version text NOT NULL,

    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'promoted', 'discarded')),
    promoted_claim_id uuid REFERENCES claims(claim_id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz,
    resolved_by text,

    CONSTRAINT promotion_names_a_claim CHECK (
        (status = 'promoted') = (promoted_claim_id IS NOT NULL)
    ),
    CONSTRAINT resolution_is_whole CHECK (
        (status = 'open') = (resolved_at IS NULL AND resolved_by IS NULL)
    )
);

CREATE INDEX extraction_candidates_open_idx ON extraction_candidates (reason, created_at)
    WHERE status = 'open';
CREATE INDEX extraction_candidates_version_idx ON extraction_candidates (version_id);
CREATE INDEX extraction_candidates_run_idx ON extraction_candidates (run_id);

-- A candidate's status may move out of 'open' once. Everything else about the
-- row - what the model said, why it was not evidence - never changes.
CREATE FUNCTION guard_extraction_candidate_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'open' THEN
        RAISE EXCEPTION 'extraction candidate % is already %', OLD.candidate_id, OLD.status;
    END IF;
    IF (NEW.candidate_id, NEW.version_id, NEW.chunk_id, NEW.run_id, NEW.predicate,
        NEW.object_text, NEW.proposed_quote, NEW.proposed_quote_sha256, NEW.reason,
        NEW.reason_detail, NEW.model, NEW.prompt_sha256, NEW.extractor_version,
        NEW.created_at)
       IS DISTINCT FROM
       (OLD.candidate_id, OLD.version_id, OLD.chunk_id, OLD.run_id, OLD.predicate,
        OLD.object_text, OLD.proposed_quote, OLD.proposed_quote_sha256, OLD.reason,
        OLD.reason_detail, OLD.model, OLD.prompt_sha256, OLD.extractor_version,
        OLD.created_at) THEN
        RAISE EXCEPTION 'only the resolution of an extraction candidate may change';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER extraction_candidates_resolve_once
BEFORE UPDATE ON extraction_candidates
FOR EACH ROW EXECUTE FUNCTION guard_extraction_candidate_update();

CREATE TRIGGER extraction_candidates_no_delete
BEFORE DELETE ON extraction_candidates
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- ---------------------------------------------------------------------------
-- 3. Processing runs learn what they were actually asked (plan §10.2)
--
-- `parameters_sha256` covers the configuration. The prompt is not configuration:
-- two runs can share every parameter and differ in the words the model saw, and
-- reproducing a result means knowing which words those were.
-- ---------------------------------------------------------------------------

ALTER TABLE processing_runs ADD COLUMN prompt_sha256 char(64)
    CHECK (prompt_sha256 IS NULL OR prompt_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE processing_runs ADD COLUMN attempt_count integer NOT NULL DEFAULT 1
    CHECK (attempt_count >= 1);
ALTER TABLE processing_runs ADD COLUMN error_detail text;

-- A terminal status is terminal. Without this, a failed run can be quietly
-- rewritten as a successful one and nothing in the store would show it.
CREATE FUNCTION guard_processing_run_status() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('succeeded', 'failed') AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION 'processing run % is already %', OLD.run_id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER processing_runs_status_is_bounded
BEFORE UPDATE ON processing_runs
FOR EACH ROW EXECUTE FUNCTION guard_processing_run_status();

-- ---------------------------------------------------------------------------
-- 4. A latent defect in migration 001, found the first time anything used it
--
-- `validate_exact_claim_evidence` is the trigger that proves a stored quotation
-- reproduces itself out of the canonical text - the guarantee the whole evidence
-- model rests on. It was written with unqualified table names, and a plpgsql body
-- resolves names when it runs, not when it is created. Inside the migration that
-- created it `search_path` was set; from an application connection it is not, so
-- the first insert of a claim from outside a migration failed with
-- `relation "document_versions" does not exist`.
--
-- Nothing was ever recorded wrongly - the trigger failed loudly rather than
-- passing anything through - and nothing had inserted a claim before slice 2.6.
-- But a guard that raises UndefinedTable is a guard that has not checked
-- anything, and it would have been discovered at the worst moment: the first
-- production extraction run.
--
-- Recreated with `SET search_path`, which pins resolution to the function rather
-- than to whoever calls it.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION validate_exact_claim_evidence() RETURNS trigger
LANGUAGE plpgsql
SET search_path = kx, public
AS $$
DECLARE
    version_text text;
    claim_version char(64);
    calculated_quote_hash text;
BEGIN
    SELECT canonical_text INTO STRICT version_text
    FROM kx.document_versions WHERE version_id = NEW.version_id;

    SELECT version_id INTO STRICT claim_version
    FROM kx.claims WHERE claim_id = NEW.claim_id;

    IF claim_version <> NEW.version_id THEN
        RAISE EXCEPTION 'claim and evidence version mismatch';
    END IF;

    calculated_quote_hash := encode(digest(convert_to(NEW.quote_text, 'UTF8'), 'sha256'), 'hex');
    IF calculated_quote_hash <> NEW.quote_sha256 THEN
        RAISE EXCEPTION 'claim evidence quote hash mismatch';
    END IF;

    IF NEW.match_status = 'exact'
       AND substr(version_text, NEW.char_start + 1, NEW.char_end - NEW.char_start)
           <> NEW.quote_text THEN
        RAISE EXCEPTION 'claim evidence is not an exact canonical-text span';
    END IF;
    RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- 5. Grants
-- ---------------------------------------------------------------------------

GRANT ALL ON claim_verdicts, extraction_candidates TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE claim_verdicts_verdict_id_seq TO radar_kx;

UPDATE metadata SET value = '7'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
