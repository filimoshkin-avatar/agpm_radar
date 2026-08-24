BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- The entities exist and the reader cannot see them
--
-- The pass filled `entities` with 6 129 names and `claim_entities` with 11 916
-- mentions, and the snapshot now carries both. None of it reaches the agent
-- mode: the serving role sees the `agent` schema and nothing else, and there is
-- no view there for either table. So UC-05's three entity modes - organisational,
-- by risk and control, by practice - still have nothing to draw.
--
-- Narrowed the same way `statement_vector` is: an entity is exposed only where
-- it was found in a statement the reader may already read. An entity named
-- solely by statements the reading threw out is not in the base, and listing its
-- name would say the base holds something it does not.
-- ---------------------------------------------------------------------------

CREATE VIEW agent.entity AS
SELECT entities.entity_id,
       entities.entity_type,
       entities.canonical_name,
       count(DISTINCT mention.claim_id) AS statements
FROM kx.entities AS entities
JOIN kx.claim_entities AS mention USING (entity_id)
WHERE EXISTS (
    SELECT 1 FROM agent.statement
    WHERE statement.claim_id = mention.claim_id
)
GROUP BY 1, 2, 3;

COMMENT ON VIEW agent.entity IS
    'Who and what the admitted statements name, with how many of them name it. '
    'An entity named only by rejected statements is not here: the base does not '
    'hold it.';

CREATE VIEW agent.statement_entity AS
SELECT mention.claim_id,
       mention.entity_id,
       entities.entity_type,
       entities.canonical_name,
       mention.role
FROM kx.claim_entities AS mention
JOIN kx.entities AS entities USING (entity_id)
WHERE EXISTS (
    SELECT 1 FROM agent.statement
    WHERE statement.claim_id = mention.claim_id
);

COMMENT ON VIEW agent.statement_entity IS
    'Which entities one statement names, and how it names them: `subject` when '
    'the statement is about it, `mentioned` when it merely cites it.';

GRANT SELECT ON agent.entity, agent.statement_entity TO radar_kb_public;

UPDATE metadata SET value = '30'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
