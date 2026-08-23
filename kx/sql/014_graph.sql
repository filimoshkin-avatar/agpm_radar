BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- The graph as an immutable projection (slice 2.11)
--
-- Everything in the graph already exists somewhere else: a concept is a wiki
-- page, an idea is a candidate the gate admitted, a claim is a span in a stored
-- document. The graph adds no facts. What it adds is a shape that can be handed
-- to a reader and to a renderer at one moment in time, and that is exactly why it
-- is a **snapshot** rather than a set of views.
--
-- A view would answer today's question with today's data, and a reader following
-- an edge tomorrow would land somewhere else. An immutable snapshot with a
-- manifest hash means a published release can point at one, and "the graph as it
-- was when this was published" is a thing that exists.
--
-- Priority 1 is concepts, ideas and claims. Priority 2 is the evidence trace -
-- claim to version to document - because a graph of assertions with no way down
-- to the span is the thing this project exists not to build.
-- ---------------------------------------------------------------------------

CREATE TABLE graph_snapshots (
    graph_snapshot_id text PRIMARY KEY,
    built_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- What the projection was taken from. A graph nobody can trace back to its
    -- inputs cannot be rebuilt or argued with.
    wiki_snapshot_id text REFERENCES wiki_snapshots(snapshot_id),
    family_decision_high_water bigint,
    node_count integer NOT NULL CHECK (node_count >= 0),
    edge_count integer NOT NULL CHECK (edge_count >= 0),
    -- SHA-256 over the sorted node and edge manifest: one value that changes
    -- when anything in the graph changes.
    manifest_sha256 char(64) NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    built_by text NOT NULL,
    notes text
);

CREATE TRIGGER graph_snapshots_immutable
BEFORE UPDATE OR DELETE ON graph_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TABLE graph_nodes (
    graph_snapshot_id text NOT NULL REFERENCES graph_snapshots(graph_snapshot_id),
    node_id text NOT NULL,
    node_kind text NOT NULL CHECK (
        node_kind IN ('concept', 'concept_claim', 'idea', 'claim', 'version', 'document', 'source_family')
    ),
    label text NOT NULL,
    -- The row this node stands for, so a reader can be taken to the real thing.
    natural_key text NOT NULL,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (graph_snapshot_id, node_id)
);

CREATE INDEX graph_nodes_kind_idx ON graph_nodes (graph_snapshot_id, node_kind);

CREATE TABLE graph_edges (
    graph_snapshot_id text NOT NULL REFERENCES graph_snapshots(graph_snapshot_id),
    edge_id bigint GENERATED ALWAYS AS IDENTITY,
    from_node_id text NOT NULL,
    to_node_id text NOT NULL,
    -- Two vocabularies deliberately kept apart. The authored one is SCHEMA.md's
    -- and is carried unchanged (P24); the structural one describes how the store
    -- is wired and is not an editorial claim about anything.
    relation text NOT NULL CHECK (
        relation IN (
            'supports', 'extends', 'constrains', 'contradicts', 'operationalizes', 'depends-on',
            'states', 'evidenced_by', 'quoted_from', 'belongs_to', 'proposed_from'
        )
    ),
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (graph_snapshot_id, edge_id),
    FOREIGN KEY (graph_snapshot_id, from_node_id)
        REFERENCES graph_nodes(graph_snapshot_id, node_id),
    FOREIGN KEY (graph_snapshot_id, to_node_id)
        REFERENCES graph_nodes(graph_snapshot_id, node_id),
    CONSTRAINT an_edge_goes_somewhere_else CHECK (from_node_id <> to_node_id)
);

CREATE INDEX graph_edges_from_idx ON graph_edges (graph_snapshot_id, from_node_id);
CREATE INDEX graph_edges_to_idx ON graph_edges (graph_snapshot_id, to_node_id);
CREATE INDEX graph_edges_relation_idx ON graph_edges (graph_snapshot_id, relation);

GRANT ALL ON graph_snapshots, graph_nodes, graph_edges TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE graph_edges_edge_id_seq TO radar_kx;

UPDATE metadata SET value = '14'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
