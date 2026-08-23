BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- The published slice, and the pointer that selects it (slice 3.1, ADR-0006 §1)
--
-- Everything built so far lives in `kx`, which holds other people's full text.
-- A reader must never be one bug away from it. So the published slice lives in
-- its own schema, `kb`, and the service that serves it gets SELECT on `kb` and
-- nothing else - that is what "its own blast radius" (P35) means in a grant.
--
-- Four properties the contract asks for:
--
--   immutable    a release's composition and counters are fixed when it is built
--   atomic       the active pointer moves in one statement
--   reversible   the previous release is still there, and rolling back is a move
--                of the same pointer, recorded like any other
--   reconcilable the slice can be compared against `kx` and the difference named
--
-- ADR-0006 §1.4: **every element of the slice carries `audience`**, and the
-- service checks it on the way out from day one. In this version it is always
-- `public`. A check added later is a check that was missing in between.
--
-- The slice is a schema, not a file (§1.3). A static file cannot filter by
-- viewer, and publishing one would make "opened = visible to everyone"
-- structural instead of merely current.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS kb;

-- ---------------------------------------------------------------------------
-- 1. The release, on the KX side
-- ---------------------------------------------------------------------------

CREATE TABLE kx.knowledge_releases (
    release_id text PRIMARY KEY,
    built_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    built_by text NOT NULL,
    -- What it was built from. A release nobody can trace to its inputs cannot be
    -- rebuilt, and cannot be argued with either.
    wiki_snapshot_id text REFERENCES kx.wiki_snapshots(snapshot_id),
    graph_snapshot_id text REFERENCES kx.graph_snapshots(graph_snapshot_id),
    family_decision_high_water bigint,
    -- Composition, fixed at build time.
    quote_count integer NOT NULL CHECK (quote_count >= 0),
    concept_count integer NOT NULL CHECK (concept_count >= 0),
    statement_count integer NOT NULL CHECK (statement_count >= 0),
    idea_count integer NOT NULL CHECK (idea_count >= 0),
    -- SHA-256 over the sorted element manifest: one value that changes when
    -- anything in the slice changes.
    state_sha256 char(64) NOT NULL CHECK (state_sha256 ~ '^[0-9a-f]{64}$'),
    notes text
);

CREATE TRIGGER knowledge_releases_immutable
BEFORE UPDATE OR DELETE ON kx.knowledge_releases
FOR EACH ROW EXECUTE FUNCTION kx.reject_immutable_mutation();

-- Append-only, with the actor. ADR-0006 §3: an editorial decision is an event,
-- not a status overwritten in place, because a status column cannot answer "who
-- decided this, and when did it change".
CREATE TABLE kx.knowledge_release_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    release_id text NOT NULL REFERENCES kx.knowledge_releases(release_id),
    action text NOT NULL CHECK (action IN ('built', 'published', 'rolled_back', 'superseded')),
    -- For a publish or a rollback: what was active before.
    previous_release_id text REFERENCES kx.knowledge_releases(release_id),
    actor text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    rationale text NOT NULL CHECK (length(rationale) BETWEEN 1 AND 4000)
);

CREATE INDEX knowledge_release_events_release_idx
    ON kx.knowledge_release_events (release_id, event_id DESC);

CREATE TRIGGER knowledge_release_events_immutable
BEFORE UPDATE OR DELETE ON kx.knowledge_release_events
FOR EACH ROW EXECUTE FUNCTION kx.reject_immutable_mutation();

-- ---------------------------------------------------------------------------
-- 2. The slice, in its own schema
--
-- Release-keyed rather than schema-per-release: several releases coexist, the
-- pointer selects, and rolling back needs no DDL. What matters for the blast
-- radius is that this schema is not `kx`, and the reader's grant says so.
-- ---------------------------------------------------------------------------

-- One row. The pointer is a row so the switch is one UPDATE, inside one
-- transaction, with no window in which nothing is published.
CREATE TABLE kb.active_release (
    only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),
    release_id text NOT NULL,
    switched_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    switched_by text NOT NULL
);

CREATE TABLE kb.releases (
    release_id text PRIMARY KEY,
    built_at timestamptz NOT NULL,
    state_sha256 char(64) NOT NULL,
    quote_count integer NOT NULL,
    concept_count integer NOT NULL,
    statement_count integer NOT NULL,
    idea_count integer NOT NULL
);

ALTER TABLE kb.active_release
    ADD CONSTRAINT active_release_exists FOREIGN KEY (release_id)
    REFERENCES kb.releases(release_id);

-- Every table below carries `audience`, checked by the service on the way out.
CREATE TABLE kb.quotes (
    release_id text NOT NULL REFERENCES kb.releases(release_id),
    quote_id uuid NOT NULL,
    audience text NOT NULL DEFAULT 'public' CHECK (audience IN ('public', 'editor')),
    original_text text NOT NULL,
    translated_text text,
    translation_is_machine boolean,
    attribution text NOT NULL,
    source_url text NOT NULL,
    caveat text,
    published_at timestamptz NOT NULL,
    PRIMARY KEY (release_id, quote_id)
);

CREATE TABLE kb.concepts (
    release_id text NOT NULL REFERENCES kb.releases(release_id),
    concept_id uuid NOT NULL,
    audience text NOT NULL DEFAULT 'public' CHECK (audience IN ('public', 'editor')),
    relative_path text NOT NULL,
    title text NOT NULL,
    language text NOT NULL,
    body text NOT NULL,
    PRIMARY KEY (release_id, concept_id)
);

CREATE TABLE kb.statements (
    release_id text NOT NULL REFERENCES kb.releases(release_id),
    statement_id uuid NOT NULL,
    concept_id uuid NOT NULL,
    audience text NOT NULL DEFAULT 'public' CHECK (audience IN ('public', 'editor')),
    statement text NOT NULL,
    claim_nature text NOT NULL,
    -- Confirmed bindings only. A proposal is not evidence, and a slice that
    -- shipped proposals as evidence would be the one failure this whole design
    -- exists to prevent.
    confirmed_evidence integer NOT NULL DEFAULT 0 CHECK (confirmed_evidence >= 0),
    PRIMARY KEY (release_id, statement_id),
    FOREIGN KEY (release_id, concept_id) REFERENCES kb.concepts(release_id, concept_id)
);

CREATE TABLE kb.statement_evidence (
    release_id text NOT NULL REFERENCES kb.releases(release_id),
    statement_id uuid NOT NULL,
    quote_id uuid NOT NULL,
    audience text NOT NULL DEFAULT 'public' CHECK (audience IN ('public', 'editor')),
    membership_class text NOT NULL,
    PRIMARY KEY (release_id, statement_id, quote_id),
    FOREIGN KEY (release_id, statement_id) REFERENCES kb.statements(release_id, statement_id),
    FOREIGN KEY (release_id, quote_id) REFERENCES kb.quotes(release_id, quote_id)
);

CREATE TABLE kb.ideas (
    release_id text NOT NULL REFERENCES kb.releases(release_id),
    idea_id uuid NOT NULL,
    audience text NOT NULL DEFAULT 'public' CHECK (audience IN ('public', 'editor')),
    title text NOT NULL,
    statement text NOT NULL,
    independent_sources integer NOT NULL,
    PRIMARY KEY (release_id, idea_id)
);

CREATE INDEX kb_quotes_release_idx ON kb.quotes (release_id, audience);
CREATE INDEX kb_statements_concept_idx ON kb.statements (release_id, concept_id);
CREATE INDEX kb_ideas_release_idx ON kb.ideas (release_id, audience);

-- ---------------------------------------------------------------------------
-- 3. The reader's blast radius
--
-- A role that can SELECT the slice and cannot see `kx` at all. This is P35 as a
-- grant rather than as an intention: the KB service can be wrong about a scope
-- and still be unable to return somebody's full text, because the rows are not
-- reachable from its connection.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_kb_reader') THEN
        CREATE ROLE radar_kb_reader NOLOGIN;
    END IF;
END
$$;

REVOKE ALL ON SCHEMA kx FROM radar_kb_reader;
GRANT USAGE ON SCHEMA kb TO radar_kb_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA kb TO radar_kb_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA kb GRANT SELECT ON TABLES TO radar_kb_reader;

GRANT ALL ON kx.knowledge_releases, kx.knowledge_release_events TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE kx.knowledge_release_events_event_id_seq TO radar_kx;
GRANT USAGE, CREATE ON SCHEMA kb TO radar_kx;
GRANT ALL ON ALL TABLES IN SCHEMA kb TO radar_kx;

UPDATE kx.metadata SET value = '15'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
