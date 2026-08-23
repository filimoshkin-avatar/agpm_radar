BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- What the agent mode may reach, as grants rather than as intentions (stage 3)
--
-- ADR-0005 §16 said KX has no public HTTP at all, and until the owner asked for
-- the agent mode that was the whole answer. It cannot be the answer any more, so
-- the question becomes what a public service is *able* to read when somebody
-- reaches it another way.
--
-- The release design (P35) answered that by copying a slice into `kb` and giving
-- the serving role SELECT on `kb` and nothing else. This is the same property
-- reached by the other route: a schema of views over what a reader is allowed to
-- see, and a role that has USAGE on that schema and no privilege anywhere in
-- `kx`. Full text, raw bodies, chunks, fetch queues, egress details, the owner's
-- decisions and everything not admitted as knowledge stay unreachable - not
-- because the service does not ask for them, but because the connection cannot.
--
-- Why views rather than a copy. A copied slice is a second thing to keep in step,
-- and the last one already drifted: 2 170 published quotations still hold the
-- text stage 0a widened. A view cannot drift from what it selects.
--
-- What is deliberately *not* here:
--
--   * `document_versions.canonical_text` - the reader gets the quotation and the
--     link, which is P32's rule; the article itself is the source's, not ours;
--   * anything the reading pass admitted as `rejected` - a vendor's connector
--     list is not knowledge and was never meant to leave the store;
--   * every table the owner decides in. The agent reads; she edits elsewhere.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS agent;
COMMENT ON SCHEMA agent IS
    'Read-only surface of the knowledge base for the public agent mode. '
    'Views only; the serving role has no privilege on kx.';


-- ---------------------------------------------------------------------------
-- 1. A statement, with everything the reader is owed beside it
--
-- Decision 7 (what kind of material), decision 1 (whose claim it was, and
-- whether this is a retelling), §2.2 (the status), stage 0a's dates and which of
-- the two dates is shown. A quotation without these is a quotation whose
-- authority the reader has to guess at.
-- ---------------------------------------------------------------------------

CREATE VIEW agent.statement AS
SELECT claims.claim_id,
       claims.normalized_text AS statement,
       evidence.quote_text,
       evidence.char_start,
       evidence.char_end,
       documents.canonical_url AS source_url,
       versions.title AS source_title,
       versions.language,
       reading.material_kind,
       reading.admission,
       reading.primary_source,
       reading.is_retelling,
       reading.valid_until,
       dates.published_on,
       dates.shown_on,
       dates.shown_kind,
       status.status,
       status.method AS status_method
FROM kx.claims AS claims
JOIN kx.claim_evidence AS evidence
  ON evidence.claim_id = claims.claim_id AND evidence.match_status = 'exact'
JOIN kx.document_versions AS versions ON versions.version_id = claims.version_id
JOIN kx.documents AS documents ON documents.document_id = versions.document_id
JOIN kx.claim_reading AS reading ON reading.claim_id = claims.claim_id
LEFT JOIN kx.document_dates AS dates ON dates.document_id = documents.document_id
LEFT JOIN kx.knowledge_status_current AS status
       ON status.unit_kind = 'claim' AND status.unit_id = claims.claim_id
WHERE reading.admission <> 'rejected';

COMMENT ON VIEW agent.statement IS
    'Every statement the reading pass admitted, with its quotation, its span, its '
    'source and its labels. A statement nobody has read yet is not here: the '
    'reader would have no way to judge it.';


-- ---------------------------------------------------------------------------
-- 2. The subject a statement sits under, and the backbone itself
-- ---------------------------------------------------------------------------

CREATE VIEW agent.statement_topic AS
SELECT placed.claim_id, topics.topic_key, topics.title, topics.level
FROM kx.claim_topics AS placed
JOIN kx.topics AS topics USING (topic_id)
WHERE topics.state = 'accepted'
  AND EXISTS (SELECT 1 FROM agent.statement WHERE statement.claim_id = placed.claim_id);

-- The path is walked here rather than stored: `topics` keeps the parent and the
-- level, and a reader navigating a three-level backbone needs the trail, not a
-- second copy of it that can fall out of step with the tree.
CREATE VIEW agent.topic AS
WITH RECURSIVE tree AS (
    SELECT topic_id, topic_key, title, level, parent_id, title AS path
    FROM kx.topics
    WHERE state = 'accepted' AND parent_id IS NULL
    UNION ALL
    SELECT child.topic_id, child.topic_key, child.title, child.level, child.parent_id,
           tree.path || ' / ' || child.title
    FROM kx.topics AS child
    JOIN tree ON tree.topic_id = child.parent_id
    WHERE child.state = 'accepted'
)
SELECT tree.topic_id,
       tree.topic_key,
       tree.title,
       tree.level,
       tree.parent_id,
       tree.path,
       (SELECT count(*) FROM agent.statement_topic AS placed
         WHERE placed.topic_key = tree.topic_key) AS statements
FROM tree;


-- ---------------------------------------------------------------------------
-- 3. Links between statements (decision 12: four types at launch)
-- ---------------------------------------------------------------------------

CREATE VIEW agent.link AS
SELECT links.from_id, links.to_id, links.link_type, links.method
FROM kx.knowledge_links AS links
WHERE links.from_kind = 'claim' AND links.to_kind = 'claim'
  AND EXISTS (SELECT 1 FROM agent.statement WHERE statement.claim_id = links.from_id)
  AND EXISTS (SELECT 1 FROM agent.statement WHERE statement.claim_id = links.to_id);


-- ---------------------------------------------------------------------------
-- 4. The authored pages, as they are (decision 13: genre by section, no cutting)
-- ---------------------------------------------------------------------------

CREATE VIEW agent.page AS
SELECT DISTINCT ON (concepts.concept_id)
       concepts.concept_id,
       concepts.relative_path,
       versions.title,
       versions.body,
       versions.language,
       versions.imported_at
FROM kx.concepts AS concepts
JOIN kx.concept_versions AS versions USING (concept_id)
ORDER BY concepts.concept_id, versions.imported_at DESC;


-- ---------------------------------------------------------------------------
-- 5. The gap map, because a base that says what it does not cover is worth more
--    than one that quietly covers nothing there (decision 8, §00)
-- ---------------------------------------------------------------------------

CREATE VIEW agent.gap AS
SELECT gaps.claim_id, gaps.missing, claims.normalized_text AS statement
FROM kx.claim_gaps AS gaps
JOIN kx.claims AS claims USING (claim_id)
WHERE EXISTS (SELECT 1 FROM agent.statement WHERE statement.claim_id = gaps.claim_id);


-- ---------------------------------------------------------------------------
-- 6. The role that serves all of this, and the little it may write
--
-- Two writes, both of which are the record of a question having been asked:
-- the egress audit (immutable by trigger) and the answer log the owner decided
-- to keep for analysis (decision 9: the chat is a chat, kept so the agent can be
-- made to answer better). Neither can be read back as anybody's article.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_kb_public') THEN
        CREATE ROLE radar_kb_public LOGIN;
    END IF;
END
$$;

REVOKE ALL ON SCHEMA kx FROM radar_kb_public;
GRANT USAGE ON SCHEMA agent TO radar_kb_public;
GRANT SELECT ON agent.statement, agent.statement_topic, agent.topic,
                agent.link, agent.page, agent.gap TO radar_kb_public;

-- The views read `kx`, so the role needs to traverse the schema - and nothing in
-- it, which is what the next line is for. A view runs with its owner's rights,
-- so this grants the ability to name the schema, not to read a table in it.
GRANT USAGE ON SCHEMA kx TO radar_kb_public;

-- What the answer path has to record. `metadata` is the schema-version gate every
-- command checks before it does anything.
GRANT SELECT ON kx.metadata TO radar_kb_public;
GRANT SELECT, INSERT ON kx.research_answers TO radar_kb_public;
GRANT SELECT, INSERT ON kx.egress_audit TO radar_kb_public;
GRANT USAGE, SELECT ON SEQUENCE kx.egress_audit_egress_id_seq TO radar_kb_public;
GRANT SELECT ON kx.embedding_models, kx.text_embeddings TO radar_kb_public;

UPDATE metadata SET value = '24'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
