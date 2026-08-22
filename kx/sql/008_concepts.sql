BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- The authored wiki, as something evidence can be attached to (P24, slice 2.5)
--
-- Owner decision P24 settled that the knowledge base is a published projection of
-- the wiki Project Manager already writes, backed by evidence from KX. This
-- migration gives that wiki a home in the store: pages become concepts, a page as
-- it stood in one wiki snapshot becomes a concept version, and the statements
-- inside it become things a span can be attached to.
--
-- Nothing here writes back into `knowledge/`. Synchronisation is one-way
-- (ADR-0008 §4), and existing pages are never rewritten (plan §16).
--
-- The shape is set by what slice 1.5 measured rather than by what the plan
-- assumed. Six SCHEMA.md conventions exist; **three pages of sixty-three carry
-- all six**, and 257 of 297 distinct second-level headings map to none of them.
-- So `concept_versions` is not six columns. It is an ordered list of sections
-- with an *optional* mapping onto a convention: what maps takes part in the
-- projection, what does not is kept as it is and is not lost. Forcing pages into
-- six sections would be the machine rewriting the author's text.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- 1. Pages
-- ---------------------------------------------------------------------------

CREATE TABLE concepts (
    concept_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The file path is part of the concept's identity (plan §10.1): the wiki has
    -- no other stable key, and a page that moves is, for now, a new concept.
    relative_path text NOT NULL,
    perimeter text NOT NULL,
    layer text NOT NULL CHECK (
        layer IN ('synthesis_page', 'source_note', 'radar_overview', 'monthly_summary', 'other')
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL,
    UNIQUE (perimeter, relative_path)
);

-- A page as it stood in one snapshot of the wiki. Immutable, and tied to the
-- snapshot so a knowledge release can say which wiki it read (P27, slice 2.5a).
CREATE TABLE concept_versions (
    concept_version_id char(64) PRIMARY KEY
        CHECK (concept_version_id ~ '^[0-9a-f]{64}$'),
    concept_id uuid NOT NULL REFERENCES concepts(concept_id),
    snapshot_id text NOT NULL REFERENCES wiki_snapshots(snapshot_id),
    title text NOT NULL,
    body text NOT NULL,
    body_sha256 char(64) NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    word_count integer NOT NULL CHECK (word_count >= 0),
    language text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    imported_by text NOT NULL,
    UNIQUE (concept_id, snapshot_id)
);

CREATE INDEX concept_versions_snapshot_idx ON concept_versions (snapshot_id);

CREATE TRIGGER concept_versions_immutable
BEFORE UPDATE OR DELETE ON concept_versions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- ---------------------------------------------------------------------------
-- 2. Sections, in the order the author wrote them
-- ---------------------------------------------------------------------------

CREATE TABLE concept_sections (
    section_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    concept_version_id char(64) NOT NULL REFERENCES concept_versions(concept_version_id),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    heading text NOT NULL,
    heading_level smallint NOT NULL CHECK (heading_level BETWEEN 1 AND 6),
    -- NULL is the ordinary case, and that is the finding rather than a gap.
    -- Matched bilingually: the wiki writes both "## Purpose" and "## Назначение",
    -- and treating only the English form as canonical would halve the measured
    -- conformance for no reason (ADR-0008 §12).
    convention text CHECK (
        convention IN (
            'purpose',
            'core_claims',
            'supporting_sources',
            'tensions',
            'implications',
            'open_questions'
        )
    ),
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end > char_start),
    UNIQUE (concept_version_id, ordinal)
);

CREATE INDEX concept_sections_convention_idx ON concept_sections (convention)
    WHERE convention IS NOT NULL;

CREATE TRIGGER concept_sections_immutable
BEFORE UPDATE OR DELETE ON concept_sections
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- ---------------------------------------------------------------------------
-- 3. Atomic statements
-- ---------------------------------------------------------------------------

CREATE TABLE concept_claims (
    concept_claim_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    concept_version_id char(64) NOT NULL REFERENCES concept_versions(concept_version_id),
    section_id uuid NOT NULL REFERENCES concept_sections(section_id),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    -- Offsets into the page body, so a binding points at a place and not at a
    -- string that may occur twice.
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end > char_start),
    statement text NOT NULL,
    statement_sha256 char(64) NOT NULL CHECK (statement_sha256 ~ '^[0-9a-f]{64}$'),
    -- The division SCHEMA.md draws (plan §10.1).
    claim_nature text NOT NULL CHECK (
        claim_nature IN ('normative', 'descriptive', 'implementation', 'open_question')
    ),
    -- How the statement was found. 34 of 63 pages already carry their statements
    -- as list items and parse mechanically; the other 29 need prose segmentation
    -- by a model with human confirmation, and that is where "the machine rewrote
    -- the author's text" would happen. Recording which is which keeps the two
    -- kinds of provenance apart forever (ADR-0008 §13).
    segmentation text NOT NULL CHECK (
        segmentation IN ('list_item', 'prose_model', 'manual')
    ),
    -- Prose segmentation proposes; a person confirms. A list item needs neither.
    confirmed_at timestamptz,
    confirmed_by text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (concept_version_id, char_start, char_end),
    CONSTRAINT confirmation_is_whole CHECK (
        (confirmed_at IS NULL) = (confirmed_by IS NULL)
    )
);

CREATE INDEX concept_claims_version_idx ON concept_claims (concept_version_id);
CREATE INDEX concept_claims_section_idx ON concept_claims (section_id);

CREATE TRIGGER concept_claims_no_delete
BEFORE DELETE ON concept_claims
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- ---------------------------------------------------------------------------
-- 4. The binding: a statement in the wiki, a span in the store
--
-- This is the new work P24 asks for. Existing pages were written without spans,
-- and "supported by evidence" has to mean a particular claim at a particular
-- offset in a particular version - not a source listed at the bottom of a page.
-- ---------------------------------------------------------------------------

CREATE TABLE concept_evidence (
    concept_claim_id uuid NOT NULL REFERENCES concept_claims(concept_claim_id),
    claim_id uuid NOT NULL REFERENCES claims(claim_id),
    -- Independence and coverage are computed inside a membership class, never
    -- across the union: the canon does not corroborate a claim about the news
    -- and the news does not corroborate the canon (ADR-0007, consequences).
    membership_class text NOT NULL,
    binding_method text NOT NULL CHECK (binding_method IN ('search_proposed', 'manual')),
    -- The score the proposal was made on, kept so a later review can see which
    -- bindings came from which floor.
    relevance numeric(8,6),
    stance text NOT NULL DEFAULT 'supports'
        CHECK (stance IN ('supports', 'contradicts', 'qualifies')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL,
    confirmed_at timestamptz,
    confirmed_by text,
    PRIMARY KEY (concept_claim_id, claim_id),
    CONSTRAINT confirmation_is_whole CHECK (
        (confirmed_at IS NULL) = (confirmed_by IS NULL)
    )
);

CREATE INDEX concept_evidence_claim_idx ON concept_evidence (claim_id);
CREATE INDEX concept_evidence_unconfirmed_idx ON concept_evidence (concept_claim_id)
    WHERE confirmed_at IS NULL;

-- What the report of "statements without evidence" reads. A statement with no
-- confirmed binding is not thereby false: it is unsupported, it is counted, and
-- it is not published as evidence-backed (ADR-0008 §2.3).
CREATE VIEW concept_claim_support AS
SELECT claims.concept_claim_id,
       claims.concept_version_id,
       claims.section_id,
       claims.claim_nature,
       claims.segmentation,
       count(evidence.claim_id) AS proposed_bindings,
       count(evidence.claim_id) FILTER (WHERE evidence.confirmed_at IS NOT NULL)
           AS confirmed_bindings
FROM kx.concept_claims AS claims
LEFT JOIN kx.concept_evidence AS evidence USING (concept_claim_id)
GROUP BY claims.concept_claim_id, claims.concept_version_id, claims.section_id,
         claims.claim_nature, claims.segmentation;

-- ---------------------------------------------------------------------------
-- 5. Making the binding search affordable
--
-- Binding walks every statement in the wiki against the quotations in the store.
-- Without an index that is a tsvector computed per claim per statement, which is
-- fine for the 324 claims extraction has produced today and is not fine at the
-- scale the perimeter implies. Both configurations, because the corpus is
-- bilingual and a Russian statement has to reach an English quotation.
-- ---------------------------------------------------------------------------

CREATE INDEX claim_evidence_quote_ru_idx ON claim_evidence
    USING gin (to_tsvector('pg_catalog.russian', quote_text));
CREATE INDEX claim_evidence_quote_en_idx ON claim_evidence
    USING gin (to_tsvector('pg_catalog.english', quote_text));

-- ---------------------------------------------------------------------------
-- 6. Grants
-- ---------------------------------------------------------------------------

GRANT ALL ON concepts, concept_versions, concept_sections, concept_claims,
             concept_evidence TO radar_kx;
GRANT SELECT ON concept_claim_support TO radar_kx;

UPDATE metadata SET value = '8'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
