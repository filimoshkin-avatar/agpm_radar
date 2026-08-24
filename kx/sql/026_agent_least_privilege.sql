BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Making the grants say what 024 claimed they said
--
-- Migration 024's own header promises that the serving role has "USAGE on the
-- `agent` schema and no privilege anywhere in `kx`", and the service unit and the
-- Caddy comment repeat it. A review of the live grants found the role reaching
-- eleven tables, not six. Five of them are in `kx`:
--
--   metadata            SELECT   needed: the schema-version gate reads it
--   research_answers    SELECT   needed: the answer cache reads its own rows
--                       INSERT   needed: decision 9 keeps the chat for analysis
--   egress_audit        INSERT   needed: a model call must leave an audit row
--                       SELECT   needed on ONE column, and only for that INSERT:
--                                `record_egress` ends `RETURNING egress_id`, and
--                                PostgreSQL checks SELECT on every column a
--                                RETURNING clause names. Revoking the table-wide
--                                SELECT without noticing that took the public
--                                answer endpoint down with "permission denied
--                                for table egress_audit" - on an INSERT
--   embedding_models    SELECT   NOT needed - nothing joins it on this path
--   text_embeddings     SELECT   needed by the semantic arm, but far too wide:
--                                it carries 19 851 chunk vectors of the whole
--                                corpus, and a chunk vector is a derivative of
--                                exactly the canonical_text the role is not
--                                allowed to read
--
-- A promise in a comment that the grants do not keep is worse than no promise:
-- the next person reads the comment. So two things happen here. The three grants
-- nothing uses are revoked, and the one that is needed is narrowed to a view
-- carrying only the vectors of statements the surface already exposes.
--
-- What stays reachable, and why each one has to be:
--   agent.*            the six views the reader sees
--   agent.statement_vector  one vector per admitted statement, for the search
--   kx.metadata        SELECT - the version gate every command runs first
--   kx.research_answers SELECT, INSERT - the answer cache and its journal
--   kx.egress_audit    INSERT, plus SELECT on `egress_id` alone - the record
--                      that a model call happened, and the id its own INSERT
--                      hands back. The other fourteen columns stay unreadable.
-- ---------------------------------------------------------------------------

-- The semantic arm needs a vector per statement, and nothing else. Restricting it
-- to `claim_evidence` is not a filter the query happens to apply - it is now the
-- only thing the role can see, so a query that forgot the filter would return
-- nothing rather than the corpus.
CREATE VIEW agent.statement_vector AS
SELECT vectors.owner_key AS claim_id,
       vectors.model_id,
       vectors.embedding
FROM kx.text_embeddings AS vectors
WHERE vectors.owner_kind = 'claim_evidence'
  AND EXISTS (
      SELECT 1 FROM agent.statement
      WHERE statement.claim_id::text = vectors.owner_key
  );

COMMENT ON VIEW agent.statement_vector IS
    'One vector per admitted statement. The chunk vectors of the corpus are not '
    'here: a chunk vector is a derivative of full text the reader may not read.';

GRANT SELECT ON agent.statement_vector TO radar_kb_public;

REVOKE SELECT ON kx.text_embeddings FROM radar_kb_public;
REVOKE SELECT ON kx.embedding_models FROM radar_kb_public;
-- Not the whole table: `record_egress` writes with `RETURNING egress_id`, and a
-- RETURNING clause needs SELECT on the columns it names. One column is the
-- smallest grant that lets the audit row be written at all.
REVOKE SELECT ON kx.egress_audit FROM radar_kb_public;
GRANT SELECT (egress_id) ON kx.egress_audit TO radar_kb_public;

UPDATE metadata SET value = '26'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
