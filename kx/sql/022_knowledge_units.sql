BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- What the owner decided the base is made of (2026-08-23, fifteen decisions)
--
-- The skeleton said what the knowledge is *about*. Everything else her document
-- asks for - how authoritative a statement is, whose claim it originally was,
-- what kind of material it came from, how long it stays true - had nowhere to go,
-- so fifteen decisions existed only as prose. This migration is where they land.
--
-- The shape follows one rule the store already lives by: **a reading is a
-- recomputation and a decision is a record.** What a model determines about a
-- claim can be determined again and is stored once, replaceable. What a person
-- decides is appended and never rewritten.
-- ---------------------------------------------------------------------------


-- ---------------------------------------------------------------------------
-- 1. What one reading of a claim determined
--
-- Four of the owner's decisions are answered by one model pass over a claim, so
-- they are one row rather than four tables:
--
--   * which of her six kinds of material it is (§5), because the reader has to
--     see **what** a statement is supported by - a fact and a forecast are not
--     the same evidence (decision 7);
--   * whose claim it originally was, and whether this is a retelling. Four
--     outlets repeating one Gartner forecast are four hosts and one source, and
--     the independence gate could not see the difference (decision 1);
--   * where it belongs: a statement about a class enters the base, a product
--     launch goes to the observatory as market chronicle, a vendor's connector
--     list is dropped (decision 3);
--   * how long it stays current, computed from the rule for its kind below.
--
-- Replaceable, not append-only: nothing here is anybody's decision, and a better
-- model re-reading the same claim should leave one answer, not two.
-- ---------------------------------------------------------------------------

CREATE TABLE claim_reading (
    claim_id uuid PRIMARY KEY REFERENCES claims(claim_id),
    material_kind text NOT NULL CHECK (
        material_kind IN ('fact', 'opinion', 'case', 'forecast', 'product_release', 'incident')
    ),
    -- Who originally said it. Empty when the outlet is itself the source: a
    -- newsroom reporting its own investigation is not retelling anybody.
    primary_source text NOT NULL DEFAULT '',
    is_retelling boolean NOT NULL,
    admission text NOT NULL CHECK (admission IN ('knowledge', 'observatory', 'rejected')),
    -- Why it was not admitted, in the reader's words. A rejection nobody can
    -- explain later is a rejection nobody can review.
    admission_note text,
    valid_until timestamptz,
    confidence numeric(4,3) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    read_by text NOT NULL,
    method text NOT NULL CHECK (method IN ('model', 'rule', 'manual')),
    read_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT a_retelling_names_its_source CHECK (
        NOT is_retelling OR length(primary_source) > 0
    )
);

CREATE INDEX claim_reading_admission_idx ON claim_reading (admission, material_kind);
CREATE INDEX claim_reading_expiry_idx ON claim_reading (valid_until)
    WHERE valid_until IS NOT NULL;
CREATE INDEX claim_reading_retelling_idx ON claim_reading (primary_source)
    WHERE is_retelling;


-- ---------------------------------------------------------------------------
-- 2. How long each kind of material stays current
--
-- The owner asked for a rule per kind rather than a date per statement: setting
-- 7 929 dates by hand is the same arithmetic that made her draw the delegation
-- boundary in the first place. Expiry queues a review and changes nothing on its
-- own (decision 11).
--
-- These intervals are a starting rule, not a finding. Each row says why it is
-- what it is, and changing one is a single UPDATE.
-- ---------------------------------------------------------------------------

CREATE TABLE material_kind_freshness (
    material_kind text PRIMARY KEY CHECK (
        material_kind IN ('fact', 'opinion', 'case', 'forecast', 'product_release', 'incident')
    ),
    -- NULL means it does not expire.
    valid_for interval,
    rationale text NOT NULL,
    decided_by text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO material_kind_freshness (material_kind, valid_for, rationale, decided_by) VALUES
    ('forecast', interval '1 year',
     'прогноз о 2027 годе перестаёт быть прогнозом, когда 2027 наступает; год — практичный шаг пересмотра',
     'radar-kx-022'),
    ('product_release', interval '6 months',
     'рынок агентных платформ меняется быстрее полугода: состав функций устаревает раньше самой новости',
     'radar-kx-022'),
    ('opinion', interval '1 year',
     'мнение остаётся сказанным, но перестаёт представлять позицию автора спустя год',
     'radar-kx-022'),
    ('fact', interval '2 years',
     'факт о состоянии практики стареет медленнее мнения и быстрее кейса',
     'radar-kx-022'),
    ('case', interval '3 years',
     'кейс внедрения остаётся поучительным дольше, чем остаётся верным его контекст',
     'radar-kx-022'),
    ('incident', interval '3 years',
     'инцидент не перестаёт быть уроком; пересматривается редко и по существу',
     'radar-kx-022');


-- ---------------------------------------------------------------------------
-- 3. Which subject a statement is about
--
-- `document_topics` from migration 019 put the subject on the whole document,
-- which was enough to compare two linking methods and is not enough to build a
-- base: an article about a product launch that mentions autonomy thresholds gets
-- one subject for all of it. This is the same shape one level down.
-- ---------------------------------------------------------------------------

CREATE TABLE claim_topics (
    claim_id uuid NOT NULL REFERENCES claims(claim_id),
    topic_id uuid NOT NULL REFERENCES topics(topic_id),
    confidence numeric(4,3) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    assigned_by text NOT NULL,
    method text NOT NULL CHECK (method IN ('model', 'embedding', 'rule', 'manual')),
    assigned_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (claim_id, topic_id)
);

CREATE INDEX claim_topics_topic_idx ON claim_topics (topic_id);


-- ---------------------------------------------------------------------------
-- 4. The gaps map
--
-- A statement the backbone has no place for is not dropped and does not grow the
-- backbone on its own: it collects here, and the owner looks when she wants
-- (decision 8, and §00 of her document). The row is what distinguishes "examined
-- and there is no place" from "not examined yet" - without it the two look
-- identical, which is how a gap becomes an oversight.
-- ---------------------------------------------------------------------------

CREATE TABLE claim_gaps (
    claim_id uuid PRIMARY KEY REFERENCES claims(claim_id),
    -- What the reader would have needed the backbone to contain.
    missing text NOT NULL,
    noted_by text NOT NULL,
    method text NOT NULL CHECK (method IN ('model', 'rule', 'manual')),
    noted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);


-- ---------------------------------------------------------------------------
-- 5. How authoritative a statement is, and the history of it becoming so
--
-- Her §2.2: "утверждение не должно незаметно переходить из сигнала в канон. Для
-- изменения статуса нужна явная редакционная процедура." Append-only, because
-- the procedure is the point: a status that can be rewritten leaves no evidence
-- that a promotion happened, which is exactly what she forbade.
--
-- The machine may propose by threshold; the owner confirms (decision 6). Both
-- sides of that are rows here, told apart by `method`.
-- ---------------------------------------------------------------------------

CREATE TABLE knowledge_status (
    status_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    unit_kind text NOT NULL CHECK (unit_kind IN ('claim', 'idea', 'concept_claim')),
    unit_id uuid NOT NULL,
    status text NOT NULL CHECK (
        status IN (
            'canon',
            'canon_adjacent',
            'operationalization',
            'external_reference',
            'observed_signal',
            'hypothesis'
        )
    ),
    -- 'rule' is the status a unit is born with, from the corpus it came out of;
    -- 'manual' is a promotion the owner signed; 'model' is a proposal, and a
    -- proposal is not in force until a manual row follows it.
    method text NOT NULL CHECK (method IN ('rule', 'model', 'manual')),
    rationale text,
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX knowledge_status_unit_idx ON knowledge_status (unit_kind, unit_id, set_at DESC);

CREATE TRIGGER knowledge_status_immutable
BEFORE UPDATE OR DELETE ON knowledge_status
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- The status in force: the latest row that a person or a rule put there. A model
-- proposal is visible in the table and never in this view, so nothing is
-- published under a status nobody granted.
CREATE VIEW knowledge_status_current AS
SELECT DISTINCT ON (unit_kind, unit_id)
       unit_kind, unit_id, status, method, set_by, set_at
FROM knowledge_status
WHERE method <> 'model'
ORDER BY unit_kind, unit_id, set_at DESC;


-- ---------------------------------------------------------------------------
-- 6. Links between units of knowledge
--
-- Her §7 lists eighteen relation types. Four live at launch (decision 12) -
-- three evidential and one navigational - because eighteen types over thousands
-- of statements is a queue her decision budget cannot carry, and because the
-- hierarchy she would otherwise need `broader-than` for is already in the
-- backbone's own parents.
--
-- The remaining fourteen stay in her document. Adding one later is a CHECK
-- constraint, not a redesign.
-- ---------------------------------------------------------------------------

CREATE TABLE knowledge_links (
    from_kind text NOT NULL CHECK (from_kind IN ('claim', 'idea', 'concept_claim')),
    from_id uuid NOT NULL,
    to_kind text NOT NULL CHECK (to_kind IN ('claim', 'idea', 'concept_claim')),
    to_id uuid NOT NULL,
    link_type text NOT NULL CHECK (
        link_type IN ('supports', 'contradicts', 'qualifies', 'related_to')
    ),
    created_by text NOT NULL,
    method text NOT NULL CHECK (method IN ('model', 'embedding', 'rule', 'manual')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (from_kind, from_id, to_kind, to_id, link_type),
    CONSTRAINT a_unit_does_not_link_to_itself CHECK (
        NOT (from_kind = to_kind AND from_id = to_id)
    )
);

CREATE INDEX knowledge_links_to_idx ON knowledge_links (to_kind, to_id, link_type);


-- ---------------------------------------------------------------------------
-- 7. Genre, on the section rather than the page
--
-- Diátaxis says an explanation, a reference and a how-to do not belong on one
-- page. 63 authored pages mix them, and the owner decided not to cut the pages
-- (decision 13): genre is a property of a section, navigation works over
-- sections, and nothing has to be rewritten to start.
--
-- A separate table because `concept_sections` is immutable - a wiki version is a
-- record of what the page said, and a genre is a judgement about it made later.
-- ---------------------------------------------------------------------------

CREATE TABLE section_genres (
    section_id uuid PRIMARY KEY REFERENCES concept_sections(section_id),
    genre text NOT NULL CHECK (
        genre IN (
            'explanation',
            'reference',
            'how_to',
            'tutorial',
            'case',
            'comparison',
            'evidence_note'
        )
    ),
    assigned_by text NOT NULL,
    method text NOT NULL CHECK (method IN ('model', 'rule', 'manual')),
    assigned_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX section_genres_genre_idx ON section_genres (genre);


-- ---------------------------------------------------------------------------
-- 8. Two more things the owner decides
--
-- Promotion of a status and review of an expired statement are her decisions,
-- not the machine's, so they are recorded the same way every other one is.
-- ---------------------------------------------------------------------------

ALTER TABLE editorial_decisions DROP CONSTRAINT editorial_decisions_object_kind_check;
ALTER TABLE editorial_decisions ADD CONSTRAINT editorial_decisions_object_kind_check CHECK (
    object_kind IN (
        'concept_evidence',
        'idea',
        'entity_alias',
        'source_family',
        'content_duplicate_cluster',
        'host_profile',
        'topic_skeleton',
        'topic',
        'publication_policy',
        'knowledge_release',
        'status_promotion',
        'freshness_review'
    )
);

GRANT ALL ON claim_reading, material_kind_freshness, claim_topics, claim_gaps,
             knowledge_status, knowledge_links, section_genres TO radar_kx;
GRANT SELECT ON knowledge_status_current TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE knowledge_status_status_id_seq TO radar_kx;

UPDATE metadata SET value = '22'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
