BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- A subject card counted statements it does not show
--
-- `agent.topic.statements` counted every placement under a subject, including
-- the 5 098 of 18 260 whose statement was admitted to the observatory rather
-- than to knowledge. The card and the wiki index both list knowledge only
-- (`agent_concept` filters `admission = 'knowledge'`), so a subject could
-- announce a number and then show a visibly smaller list, with nothing on the
-- page to explain the difference.
--
-- The number is the one the reader can check. The observatory has its own tab
-- and its own cut by class of event (decision 4); it is not what a wiki subject
-- is counting.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW agent.topic AS
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
       (SELECT count(*)
          FROM agent.statement_topic AS placed
          JOIN agent.statement AS statement USING (claim_id)
         WHERE placed.topic_key = tree.topic_key
           AND statement.admission = 'knowledge') AS statements
FROM tree;

COMMENT ON VIEW agent.topic IS
    'The accepted backbone, with how many *knowledge* statements stand under '
    'each subject - the same population the subject card lists.';

UPDATE metadata SET value = '28'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
