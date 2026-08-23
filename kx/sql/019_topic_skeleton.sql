BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- The topic skeleton (slice 2.5в, prerequisite)
--
-- The owner asked what backbone the knowledge base is being built on, and the
-- honest answer was: none. Statements were matched to claims by shared words,
-- with nothing requiring a match to be about the same subject, and that is the
-- root of the connection quality they noticed.
--
-- A backbone exists - twice, in prose, in two wiki pages that do not quite agree:
--
--   `wiki/overview/ontological-structure.md` gives four categories at level 1
--   (subjects, model architecture, management mechanisms, evaluation frameworks)
--   and says levels 2 and 3 exist without enumerating them.
--
--   `wiki/overview/agpm-overview.md` gives seven layers of the model, and the
--   wiki's own directory layout half-matches those - `data/`, `maturity/`,
--   `risks/`, `market/` and `open-questions/` are layers with no pages at all.
--
-- Neither is in the store. This migration is where the chosen one goes, once a
-- person has chosen it: a topic is an editorial fact about what the field is
-- made of, and inferring it from a corpus is how a knowledge base ends up
-- organised around whatever was written about most.
-- ---------------------------------------------------------------------------

CREATE TABLE topics (
    topic_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_key text NOT NULL UNIQUE CHECK (topic_key ~ '^[a-z0-9][a-z0-9-]{1,80}$'),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    -- Which skeleton it came from, so a later reader can see the shape of the
    -- decision rather than only its result.
    source text NOT NULL CHECK (
        source IN ('agpm_ontology', 'agpm_model_layers', 'wiki_sections', 'authored')
    ),
    -- Level 1 is a category, level 2 a subgroup, level 3 a concrete element
    -- (ontological-structure.md).
    level smallint NOT NULL CHECK (level BETWEEN 1 AND 3),
    parent_id uuid REFERENCES topics(topic_id),
    description text,
    state text NOT NULL DEFAULT 'proposed'
        CHECK (state IN ('proposed', 'accepted', 'rejected')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL,
    CONSTRAINT a_level_one_topic_has_no_parent CHECK ((level = 1) = (parent_id IS NULL))
);

CREATE INDEX topics_state_idx ON topics (state, level);
CREATE INDEX topics_parent_idx ON topics (parent_id);

-- Which topics a document is about, and which a wiki statement is about. Both
-- are needed before a binding can be required to stay inside a subject.
CREATE TABLE document_topics (
    document_id char(64) NOT NULL REFERENCES documents(document_id),
    topic_id uuid NOT NULL REFERENCES topics(topic_id),
    confidence numeric(4,3) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    assigned_by text NOT NULL,
    method text NOT NULL CHECK (method IN ('model', 'embedding', 'rule', 'manual')),
    assigned_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (document_id, topic_id)
);

CREATE TABLE concept_claim_topics (
    concept_claim_id uuid NOT NULL REFERENCES concept_claims(concept_claim_id),
    topic_id uuid NOT NULL REFERENCES topics(topic_id),
    confidence numeric(4,3) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    assigned_by text NOT NULL,
    method text NOT NULL CHECK (method IN ('model', 'embedding', 'rule', 'manual')),
    assigned_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (concept_claim_id, topic_id)
);

CREATE INDEX document_topics_topic_idx ON document_topics (topic_id);
CREATE INDEX concept_claim_topics_topic_idx ON concept_claim_topics (topic_id);

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
        'knowledge_release'
    )
);

GRANT ALL ON topics, document_topics, concept_claim_topics TO radar_kx;

UPDATE metadata SET value = '19'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
