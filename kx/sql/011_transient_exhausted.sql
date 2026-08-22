BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- A failure that says nothing about the document
--
-- The gap queue of migration 010 was planned against production the same day and
-- the vocabulary did not survive contact. Two of its five escalation rules named
-- error codes the fetcher never emits (`parser_no_text`, `empty_body`) and could
-- not have fired. And 79 documents whose only problem is a timeout, a network
-- error or a 5xx were filed under "ladder exhausted, a person must decide" -
-- which is how a gap queue stops being read.
--
-- A timeout is not a finding about a page. The attempts ran out; a requeue is the
-- whole action, and it is the machine's. That is a different answer from every
-- other terminal reason here and it needs its own name.
-- ---------------------------------------------------------------------------

ALTER TABLE fetch_queue DROP CONSTRAINT fetch_queue_terminal_reason_check;
ALTER TABLE fetch_queue ADD CONSTRAINT fetch_queue_terminal_reason_check CHECK (
    terminal_reason IS NULL OR terminal_reason IN (
        'obtained',
        'removed_at_source',
        'requires_credentials',
        'no_public_text',
        'blocked_by_host',
        'ladder_exhausted',
        'refused_by_policy',
        'transient_exhausted'
    )
);

UPDATE metadata SET value = '11'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
