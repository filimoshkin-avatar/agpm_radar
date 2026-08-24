BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- The graph did not contain the knowledge
--
-- 11 466 nodes and 21 235 edges, and every edge is provenance: a statement to
-- the version it was cut from, a statement to the document, a concept to the
-- statement that states it, an outlet to its family. Useful, and not what UC-05
-- asks for.
--
-- Everything stages 0b-2 built lives outside it: 229 accepted subjects, 18 325
-- placements, 15 414 links of four types. A reader opening "the graph" saw
-- where a quotation came from, never what the base says about it - so the
-- "доказательный" mode showed the trace of one citation and the other five
-- modes had nothing at all to draw.
--
-- Two vocabularies widen here, and each one keeps its meaning:
--
--   node_kind += topic     the backbone, which is the owner's own structure
--                 entity   the layer UC-05's three remaining modes need; the
--                          table exists and is empty, so this is the CHECK
--                          getting out of the way before the pass fills it
--
--   relation  += qualifies, related_to   two of the four link types decision 12
--                          chose. `supports` and `contradicts` were already in
--                          the authored vocabulary and carry straight over
--               about      statement -> subject. Structural: it asserts nothing
--                          about the world, only that a reading placed one
--               mentions   statement -> entity, structural for the same reason
--
-- `about` and `mentions` are deliberately structural rather than authored. An
-- authored edge is somebody's claim about the world (SCHEMA.md, P24); "this
-- statement was placed under that subject" is a fact about the store.
-- ---------------------------------------------------------------------------

ALTER TABLE graph_nodes DROP CONSTRAINT graph_nodes_node_kind_check;
ALTER TABLE graph_nodes ADD CONSTRAINT graph_nodes_node_kind_check CHECK (
    node_kind IN (
        'concept', 'concept_claim', 'idea', 'claim', 'version', 'document',
        'source_family', 'topic', 'entity'
    )
);

ALTER TABLE graph_edges DROP CONSTRAINT graph_edges_relation_check;
ALTER TABLE graph_edges ADD CONSTRAINT graph_edges_relation_check CHECK (
    relation IN (
        'supports', 'extends', 'constrains', 'contradicts', 'operationalizes',
        'depends-on', 'qualifies', 'related_to',
        'states', 'evidenced_by', 'quoted_from', 'belongs_to', 'proposed_from',
        'about', 'mentions'
    )
);

-- Which statements an entity was found in. `claims.subject_entity_id` holds one
-- subject per statement and cannot carry "this sentence names Gartner, the EU AI
-- Act and a PMO" - which is the normal case.
CREATE TABLE claim_entities (
    claim_id uuid NOT NULL REFERENCES claims(claim_id),
    entity_id uuid NOT NULL REFERENCES entities(entity_id),
    -- How the entity stands in the sentence. A statement *about* Gartner and one
    -- that merely cites Gartner are different facts, and the graph would draw
    -- them the same way.
    role text NOT NULL CHECK (role IN ('subject', 'mentioned')),
    -- The exact words the sentence used, so an alias can be traced to its
    -- statement rather than asserted.
    surface_form text NOT NULL,
    found_by text NOT NULL,
    method text NOT NULL CHECK (method IN ('model', 'rule', 'manual')),
    found_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (claim_id, entity_id, role)
);

CREATE INDEX claim_entities_entity_idx ON claim_entities (entity_id);

COMMENT ON TABLE claim_entities IS
    'Which entities a statement names, and how. Many per statement: one sentence '
    'commonly names an analyst house, a regulation and a role at once.';

-- That a statement was read for entities, whatever the answer was.
--
-- Without this row, "read and named nothing" and "not read yet" look identical,
-- and every run pays again for the statements that name nothing - which is the
-- majority. It is the same defect migration 025 fixed for linking, written down
-- here before it can happen a second time.
CREATE TABLE entity_reads (
    claim_id uuid PRIMARY KEY REFERENCES claims(claim_id),
    read_by text NOT NULL,
    read_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE entity_reads IS
    'One row per statement the entity pass looked at. A statement naming nothing '
    'is a result, not a gap, and must not be offered again.';

UPDATE metadata SET value = '29'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
