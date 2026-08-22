BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Source independence (defect D13, ADR-0007)
--
-- Two rows in `documents` are two documents, and until now nothing in the schema
-- said whether they are two pieces of evidence. The radar's perimeter is news,
-- and news propagates by reprint: a rating that counts "how many sources say
-- this" reads a press release's distribution list as agreement among twelve
-- observers.
--
-- Four entities, and one rule that shapes all of them: a family is an editorial
-- fact, not a computed one (ADR-0007 §11). The machine proposes, a person
-- confirms, and the confirmation is an append-only event - so a correction next
-- month cannot silently change what a score meant last month.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- 1. Families
-- ---------------------------------------------------------------------------

CREATE TABLE source_families (
    family_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- A stable slug so a family can be named in a batch file, a test and a
    -- report without carrying a uuid around.
    family_key text NOT NULL UNIQUE CHECK (family_key ~ '^[a-z0-9][a-z0-9-]{1,80}$'),
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
    family_kind text NOT NULL CHECK (
        family_kind IN ('owner', 'editorial_desk', 'syndication_channel')
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL
);

-- Append-only. The family's version is the id of its latest decision, and that
-- is what a score stores alongside its independence verdict (ADR-0007 §4): a
-- rating that changes because a family was edited afterwards is not a rating
-- anybody can reason about.
--
-- One decision covers one family and the whole membership it was confirmed
-- with, because per-document confirmation would not fit the 15-30 minutes a day
-- of P15 - the perimeter alone spans 198 hosts (ADR-0007 §11a).
CREATE TABLE source_family_decisions (
    decision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    family_id uuid NOT NULL REFERENCES source_families(family_id),
    -- The weekly batch this decision came from. Several families share one.
    batch_id uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('confirmed', 'corrected', 'retired')),
    decided_by text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    rationale text NOT NULL CHECK (length(rationale) BETWEEN 1 AND 4000),
    -- Hash of the membership this decision covers, so a later reader can tell
    -- whether the assignment rows still are the ones that were confirmed.
    members_sha256 char(64) NOT NULL CHECK (members_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX source_family_decisions_family_idx
    ON source_family_decisions (family_id, decision_id DESC);
CREATE INDEX source_family_decisions_batch_idx ON source_family_decisions (batch_id);

CREATE TRIGGER source_family_decisions_immutable
BEFORE UPDATE OR DELETE ON source_family_decisions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- Membership is append-only for the same reason the decision is: a document
-- moved between families last week must not rewrite what it meant last month.
-- The current assignment is the highest assignment_id for the document.
CREATE TABLE document_source_family (
    assignment_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id char(64) NOT NULL REFERENCES documents(document_id),
    family_id uuid NOT NULL REFERENCES source_families(family_id),
    decision_id bigint NOT NULL REFERENCES source_family_decisions(decision_id),
    assigned_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX document_source_family_document_idx
    ON document_source_family (document_id, assignment_id DESC);
CREATE INDEX document_source_family_family_idx ON document_source_family (family_id);

CREATE TRIGGER document_source_family_immutable
BEFORE UPDATE OR DELETE ON document_source_family
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE VIEW document_source_family_current AS
SELECT DISTINCT ON (assignment.document_id)
       assignment.document_id,
       assignment.family_id,
       assignment.decision_id,
       assignment.assigned_at,
       families.family_key,
       families.family_kind,
       decisions.action AS decision_action
FROM document_source_family AS assignment
JOIN source_families AS families USING (family_id)
JOIN source_family_decisions AS decisions USING (decision_id)
ORDER BY assignment.document_id, assignment.assignment_id DESC;

-- ---------------------------------------------------------------------------
-- 2. Content duplicate clusters
--
-- Note what `formation_method` does not offer: `shared_primary_source`. Two
-- articles citing one press release is a hint, not a cluster on its own
-- (ADR-0007 §10), so it can appear as evidence and can never be the thing that
-- formed a cluster. The type system carries the rule.
-- ---------------------------------------------------------------------------

CREATE TABLE content_duplicate_clusters (
    cluster_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_kind text NOT NULL CHECK (
        cluster_kind IN ('reprint', 'syndication', 'shared_press_release', 'shared_primary_source')
    ),
    formation_method text NOT NULL CHECK (
        formation_method IN ('canonical_text_hash', 'shingle_overlap', 'manual')
    ),
    -- Recorded with the cluster, so a later review can tell which clusters were
    -- formed under which rule instead of guessing (ADR-0007, consequences).
    shingle_threshold numeric(4,3) CHECK (
        shingle_threshold IS NULL OR (shingle_threshold > 0 AND shingle_threshold <= 1)
    ),
    shingle_width smallint CHECK (shingle_width IS NULL OR shingle_width BETWEEN 2 AND 32),
    proposed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    proposed_by text NOT NULL,
    batch_id uuid,
    confirmed_at timestamptz,
    confirmed_by text,
    CONSTRAINT shingle_clusters_state_their_rule CHECK (
        (formation_method = 'shingle_overlap')
        = (shingle_threshold IS NOT NULL AND shingle_width IS NOT NULL)
    ),
    CONSTRAINT confirmation_is_whole CHECK (
        (confirmed_at IS NULL) = (confirmed_by IS NULL)
    )
);

CREATE INDEX content_duplicate_clusters_batch_idx ON content_duplicate_clusters (batch_id);

CREATE TABLE content_duplicate_cluster_members (
    cluster_id uuid NOT NULL REFERENCES content_duplicate_clusters(cluster_id),
    document_id char(64) NOT NULL REFERENCES documents(document_id),
    PRIMARY KEY (cluster_id, document_id)
);

CREATE INDEX content_duplicate_cluster_members_document_idx
    ON content_duplicate_cluster_members (document_id);

CREATE TABLE duplicate_evidence (
    evidence_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cluster_id uuid NOT NULL REFERENCES content_duplicate_clusters(cluster_id),
    evidence_kind text NOT NULL CHECK (
        evidence_kind IN ('canonical_text_hash', 'shingle_overlap', 'shared_cited_primary_source')
    ),
    left_document_id char(64) NOT NULL REFERENCES documents(document_id),
    right_document_id char(64) NOT NULL REFERENCES documents(document_id),
    -- Jaccard overlap for shingles; 1 for an identical hash; NULL for a hint.
    similarity numeric(5,4) CHECK (similarity IS NULL OR (similarity >= 0 AND similarity <= 1)),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    recorded_by text NOT NULL,
    CONSTRAINT evidence_is_between_two_documents CHECK (left_document_id <> right_document_id),
    CONSTRAINT hash_evidence_is_certain CHECK (
        evidence_kind <> 'canonical_text_hash' OR similarity = 1
    ),
    CONSTRAINT shingle_evidence_states_its_overlap CHECK (
        evidence_kind <> 'shingle_overlap' OR similarity IS NOT NULL
    )
);

CREATE INDEX duplicate_evidence_cluster_idx ON duplicate_evidence (cluster_id);

CREATE TRIGGER duplicate_evidence_immutable
BEFORE UPDATE OR DELETE ON duplicate_evidence
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

-- ---------------------------------------------------------------------------
-- 3. The counting rule, in one place
--
-- Two documents of one family are one confirmation; documents of one confirmed
-- cluster are one confirmation regardless of family; and a document with no
-- confirmed family is unknown, which never satisfies a "two independent
-- sources" requirement (ADR-0007 §2 and §12). Fail-closed: the default is not
-- "presumed independent".
--
-- It is a function rather than three copies of a GROUP BY in scoring, the graph
-- and the idea gate, because ADR-0007 §5 says those three apply the same rules
-- and three copies would eventually be three rules.
-- ---------------------------------------------------------------------------

CREATE FUNCTION independence_report(document_ids char(64)[])
RETURNS TABLE (
    documents_considered integer,
    independent_sources integer,
    unknown_documents integer,
    collapsed_by_family integer,
    collapsed_by_cluster integer
)
-- Schema-qualified inside the body: the function runs with the caller's
-- search_path, not the one this migration set.
LANGUAGE sql STABLE AS $$
WITH considered AS (
    SELECT DISTINCT unnest(document_ids) AS document_id
), grouped AS (
    SELECT considered.document_id,
           family.family_id,
           -- A confirmed cluster collapses its members whatever their families.
           -- An unconfirmed proposal does not: the machine has not been given
           -- authority to reduce a count on its own (ADR-0007 §11).
           (SELECT min(member.cluster_id::text)
            FROM kx.content_duplicate_cluster_members AS member
            JOIN kx.content_duplicate_clusters AS cluster USING (cluster_id)
            WHERE member.document_id = considered.document_id
              AND cluster.confirmed_at IS NOT NULL) AS cluster_key
    FROM considered
    LEFT JOIN kx.document_source_family_current AS family
           ON family.document_id = considered.document_id
          AND family.decision_action <> 'retired'
)
SELECT count(*)::integer AS documents_considered,
       count(DISTINCT coalesce(cluster_key, 'family:' || family_id::text))
           FILTER (WHERE family_id IS NOT NULL)::integer AS independent_sources,
       count(*) FILTER (WHERE family_id IS NULL)::integer AS unknown_documents,
       (count(*) FILTER (WHERE family_id IS NOT NULL)
        - count(DISTINCT family_id) FILTER (WHERE family_id IS NOT NULL))::integer
           AS collapsed_by_family,
       (count(*) FILTER (WHERE cluster_key IS NOT NULL)
        - count(DISTINCT cluster_key) FILTER (WHERE cluster_key IS NOT NULL))::integer
           AS collapsed_by_cluster
FROM grouped;
$$;

-- ---------------------------------------------------------------------------
-- 4. Grants
-- ---------------------------------------------------------------------------

GRANT ALL ON source_families, source_family_decisions, document_source_family,
             content_duplicate_clusters, content_duplicate_cluster_members,
             duplicate_evidence TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE source_family_decisions_decision_id_seq TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE document_source_family_assignment_id_seq TO radar_kx;
GRANT USAGE, SELECT ON SEQUENCE duplicate_evidence_evidence_id_seq TO radar_kx;
GRANT SELECT ON document_source_family_current TO radar_kx;
GRANT EXECUTE ON FUNCTION independence_report(char(64)[]) TO radar_kx;

UPDATE metadata SET value = '5'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
