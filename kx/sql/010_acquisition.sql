BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Acquisition as a subsystem, not a single request (slice 2.3, plan §11.5-11.6)
--
-- Full text exists for 5 979 of 8 313 documents. The 2 334 that do not have it
-- are not one problem: 1 745 are Reddit refusing a robots-respecting client, some
-- are pages that only yield text once rendered, some are gone from the web and
-- present in an archive, and some genuinely have no public text.
--
-- Today a fetch either works or the document fails, terminally. What makes this a
-- subsystem instead is four things, and this migration carries three of them:
--
--   the host profile   - which rungs are worth trying here, at what pace, with
--                        which headers, and whether robots is a routing signal or
--                        a wall (P11)
--   the ladder state   - which rung a document is on and which it has already
--                        tried, so a failure escalates instead of ending
--   terminal states    - that mean something. "failed" is not a finding;
--                        "removed at source" and "requires credentials" are
--   the gap queue      - what is missing, why, and whose move it is
--
-- The fourth, automatic escalation, is code.
--
-- Nothing here changes what the fetcher does today. A host with no profile gets
-- the default profile, and the default profile is exactly the current behaviour:
-- one rung, respect robots, the global pace. The subsystem is inert until
-- somebody writes a profile, which is the point - a change to how other people's
-- servers are treated should be a decision somebody made, not a deployment.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- 1. Host profiles
-- ---------------------------------------------------------------------------

CREATE TABLE host_profiles (
    host text PRIMARY KEY CHECK (host = lower(host) AND length(host) BETWEEN 1 AND 253),
    -- The rungs worth trying for this host, in order. NULL means the default
    -- ladder. An empty array means "do not fetch from this host at all", which is
    -- a decision somebody can make and the fetcher will honour.
    rung_order text[],
    -- Pace. NULL means the global setting.
    min_interval_seconds numeric(6,3) CHECK (
        min_interval_seconds IS NULL OR min_interval_seconds > 0
    ),
    max_in_flight integer CHECK (max_in_flight IS NULL OR max_in_flight > 0),
    -- P11 made robots a routing signal rather than a terminal state, but only
    -- where somebody decided that for a host and said why. Deciding it globally
    -- is what `RADAR_KX_RESPECT_ROBOTS` does today and it is the wrong grain.
    robots_policy text NOT NULL DEFAULT 'respect'
        CHECK (robots_policy IN ('respect', 'override_recorded')),
    request_headers jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Why this profile exists. A profile without a reason is a profile nobody can
    -- review later, and these govern how we treat somebody else's server.
    rationale text NOT NULL CHECK (length(rationale) BETWEEN 1 AND 4000),
    decided_by text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT an_override_says_why CHECK (
        robots_policy <> 'override_recorded' OR length(rationale) >= 20
    )
);

-- ---------------------------------------------------------------------------
-- 2. Where a document is on the ladder
--
-- Kept in `fetch_queue` rather than beside it: the queue already owns attempts,
-- status and the last error, and a second table would be a second answer to
-- "what is happening with this document".
-- ---------------------------------------------------------------------------

ALTER TABLE fetch_queue ADD COLUMN current_rung text NOT NULL DEFAULT 'network'
    CHECK (
        current_rung IN (
            'network',
            'network_browser_headers',
            'network_robots_override',
            'source_specific_parse',
            'browser_render',
            'web_archive',
            'operator_artifact'
        )
    );

ALTER TABLE fetch_queue ADD COLUMN rungs_tried text[] NOT NULL DEFAULT '{}'::text[];

-- A terminal state that says something. `status = 'failed'` records that an
-- attempt did not work; this records what we now believe about the document, and
-- the two are different questions.
ALTER TABLE fetch_queue ADD COLUMN terminal_reason text CHECK (
    terminal_reason IS NULL OR terminal_reason IN (
        'obtained',
        'removed_at_source',
        'requires_credentials',
        'no_public_text',
        'blocked_by_host',
        'ladder_exhausted',
        'refused_by_policy'
    )
);

-- Whose move it is. A gap queue without this is a list nobody owns.
ALTER TABLE fetch_queue ADD COLUMN next_action_owner text CHECK (
    next_action_owner IS NULL OR next_action_owner IN ('machine', 'operator', 'owner')
);

CREATE INDEX fetch_queue_gap_idx ON fetch_queue (terminal_reason, next_action_owner)
    WHERE terminal_reason IS NOT NULL AND terminal_reason <> 'obtained';

-- ---------------------------------------------------------------------------
-- 3. The gap queue
-- ---------------------------------------------------------------------------

CREATE VIEW acquisition_gap_queue AS
SELECT queue.document_id,
       documents.canonical_url,
       queue.status,
       queue.current_rung,
       queue.rungs_tried,
       queue.terminal_reason,
       queue.next_action_owner,
       queue.attempt_count,
       queue.last_http_status,
       queue.last_error_code,
       queue.updated_at
FROM kx.fetch_queue AS queue
JOIN kx.documents AS documents USING (document_id)
WHERE NOT EXISTS (
    SELECT 1 FROM kx.document_versions AS versions
    WHERE versions.document_id = queue.document_id AND versions.is_complete
);

-- ---------------------------------------------------------------------------
-- 4. Grants
-- ---------------------------------------------------------------------------

GRANT ALL ON host_profiles TO radar_kx;
GRANT SELECT ON acquisition_gap_queue TO radar_kx;

UPDATE metadata SET value = '10'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
