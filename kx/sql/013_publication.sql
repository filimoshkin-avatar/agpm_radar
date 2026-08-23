BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Translations and automatic publication of the structural layer (slice 2.8)
--
-- Owner decision P19 splits the two publication paths. Quotations, figures and
-- translations publish **automatically**, with no manual and no batch approval
-- gate, when five conditions hold at once (plan §8.4). Authored wiki text and the
-- phrasing of insights stay under the owner's approval (P4) and none of this
-- touches them.
--
-- The five conditions, and what each one is defended by here:
--
--   1. the original quotation matches an immutable span exactly
--        - the span is copied into the row and a trigger re-derives it
--   2. coordinates, hash, URL and provenance are valid
--        - `version_publication_block` already answers this (migration 004)
--   3. figures, dates, units and proper names pass deterministic checks
--        - `invariant_report`, computed in code and stored with the translation
--   4. the translation is shown **with the available original** and marked machine
--        - `published_quotes` carries the original text, not a reference to it
--   5. source independence passes where it applies
--        - the verdict of slice 2.9, recorded on the row
--
-- Any condition that fails sends the item to quarantine **with the reason and
-- with what would clear it**. A quarantine queue that says only "rejected" is a
-- queue nobody can work.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- 1. Translations
-- ---------------------------------------------------------------------------

CREATE TABLE quote_translations (
    translation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id uuid NOT NULL REFERENCES claims(claim_id),
    version_id char(64) NOT NULL REFERENCES document_versions(version_id),
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end > char_start),
    -- The original is copied in, not referenced. Plan §8.5 rule 15: the original
    -- is always available, and without a stored original a translation is not
    -- published. A join that could fail is not "always available".
    original_text text NOT NULL,
    source_language text NOT NULL,
    target_language text NOT NULL,
    translated_text text NOT NULL,
    -- A model id, or a person. `is_machine` decides whether the reader is told.
    translator text NOT NULL,
    is_machine boolean NOT NULL,
    prompt_sha256 char(64) CHECK (prompt_sha256 IS NULL OR prompt_sha256 ~ '^[0-9a-f]{64}$'),
    state text NOT NULL DEFAULT 'proposed'
        CHECK (state IN ('proposed', 'verified', 'rejected')),
    -- What was checked and what matched, so a later reader does not have to
    -- recompute it to know why this was allowed through.
    invariant_report jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL,
    CONSTRAINT a_machine_translation_names_its_prompt CHECK (
        NOT is_machine OR prompt_sha256 IS NOT NULL
    ),
    CONSTRAINT the_translation_is_not_the_original CHECK (
        source_language <> target_language
    ),
    UNIQUE (claim_id, char_start, char_end, target_language, translator, prompt_sha256)
);

CREATE INDEX quote_translations_claim_idx ON quote_translations (claim_id);

-- The stored original has to be the span it claims to be. Without this the whole
-- chain rests on a copy nobody rechecked.
CREATE FUNCTION validate_translation_original() RETURNS trigger
LANGUAGE plpgsql
SET search_path = kx, public
AS $$
DECLARE
    version_text text;
BEGIN
    SELECT canonical_text INTO STRICT version_text
    FROM kx.document_versions WHERE version_id = NEW.version_id;
    IF substr(version_text, NEW.char_start + 1, NEW.char_end - NEW.char_start)
       <> NEW.original_text THEN
        RAISE EXCEPTION 'the stored original is not the span it names';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER quote_translations_original_is_the_span
BEFORE INSERT OR UPDATE ON quote_translations
FOR EACH ROW EXECUTE FUNCTION validate_translation_original();

-- ---------------------------------------------------------------------------
-- 2. What was published
-- ---------------------------------------------------------------------------

CREATE TABLE published_quotes (
    published_quote_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id uuid NOT NULL REFERENCES claims(claim_id),
    version_id char(64) NOT NULL REFERENCES document_versions(version_id),
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end > char_start),
    original_text text NOT NULL,
    translation_id uuid REFERENCES quote_translations(translation_id),
    quote_chars integer NOT NULL CHECK (quote_chars > 0),
    -- P32: attribution and a link, one rule for everything (P34 removed the
    -- differentiation by source type).
    attribution text NOT NULL CHECK (length(attribution) > 0),
    source_url text NOT NULL CHECK (length(source_url) > 0),
    -- ADR-0004 rule 21a: text from a web archive whose snapshot was not preserved
    -- publishes with a caveat rather than being withheld.
    caveat text,
    -- P19 or a person. Recorded, because "who let this out" is the first question
    -- anybody asks about a published quotation.
    published_automatically boolean NOT NULL,
    decided_by text,
    independence_sources integer CHECK (
        independence_sources IS NULL OR independence_sources >= 0
    ),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT a_manual_publication_names_who CHECK (
        published_automatically OR decided_by IS NOT NULL
    ),
    UNIQUE (claim_id, char_start, char_end, translation_id)
);

CREATE INDEX published_quotes_claim_idx ON published_quotes (claim_id);

CREATE TRIGGER published_quotes_immutable
BEFORE UPDATE OR DELETE ON published_quotes
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER published_quotes_original_is_the_span
BEFORE INSERT ON published_quotes
FOR EACH ROW EXECUTE FUNCTION validate_translation_original();

-- ---------------------------------------------------------------------------
-- 3. Quarantine
--
-- The vocabulary is the five conditions of §8.4 plus the two length and
-- publication rules that sit beside them. `what_would_clear_it` is not
-- decoration: a queue that says only "rejected" is a queue nobody can work.
-- ---------------------------------------------------------------------------

CREATE TABLE publication_quarantine (
    quarantine_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id uuid NOT NULL REFERENCES claims(claim_id),
    translation_id uuid REFERENCES quote_translations(translation_id),
    failed_condition text NOT NULL CHECK (
        failed_condition IN (
            'quote_is_not_an_exact_span',
            'provenance_invalid',
            'invariant_mismatch',
            'original_unavailable',
            'source_independence',
            'quote_longer_than_a_paragraph',
            'publication_blocked'
        )
    ),
    detail text NOT NULL,
    what_would_clear_it text NOT NULL CHECK (length(what_would_clear_it) > 0),
    quarantined_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz,
    resolved_by text,
    CONSTRAINT resolution_is_whole CHECK (
        (resolved_at IS NULL) = (resolved_by IS NULL)
    )
);

CREATE INDEX publication_quarantine_open_idx
    ON publication_quarantine (failed_condition, quarantined_at)
    WHERE resolved_at IS NULL;

CREATE TRIGGER publication_quarantine_no_delete
BEFORE DELETE ON publication_quarantine
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- ---------------------------------------------------------------------------
-- 4. Alias proposals (P36)
--
-- An unregistered spelling does not block a quotation. The name is shown in the
-- original, and a proposal goes into a queue with no deadline. When an alias is
-- approved it applies to later publications - never retroactively, because a
-- published quotation is immutable.
-- ---------------------------------------------------------------------------

CREATE TABLE entity_alias_proposals (
    proposal_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    original_form text NOT NULL,
    proposed_form text NOT NULL,
    language text NOT NULL,
    seen_in_translation uuid REFERENCES quote_translations(translation_id),
    occurrences integer NOT NULL DEFAULT 1 CHECK (occurrences > 0),
    first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- Append-only decision, with the actor (ADR-0006 §3).
    decided_at timestamptz,
    decided_by text,
    decision text CHECK (decision IN ('accepted', 'rejected')),
    entity_id uuid REFERENCES entities(entity_id),
    CONSTRAINT a_decision_is_whole CHECK (
        (decided_at IS NULL) = (decided_by IS NULL)
        AND (decided_at IS NULL) = (decision IS NULL)
    ),
    CONSTRAINT an_accepted_alias_names_its_entity CHECK (
        decision IS DISTINCT FROM 'accepted' OR entity_id IS NOT NULL
    ),
    UNIQUE (original_form, proposed_form, language)
);

GRANT ALL ON quote_translations, published_quotes, publication_quarantine,
             entity_alias_proposals TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE publication_quarantine_quarantine_id_seq TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE entity_alias_proposals_proposal_id_seq TO radar_kx;

UPDATE metadata SET value = '13'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
