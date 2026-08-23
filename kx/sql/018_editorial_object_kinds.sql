BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Two object kinds the journal did not know about
--
-- Migration 016 listed the kinds of editorial decision that existed when it was
-- written. Extending the editor to cover everything waiting for the owner added
-- two more: a duplicate cluster confirmed, and a host policy decided. Both are
-- decisions with an actor and both belong in the same journal, which is the whole
-- argument for having one.
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
        'publication_policy',
        'knowledge_release'
    )
);

UPDATE metadata SET value = '18'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
