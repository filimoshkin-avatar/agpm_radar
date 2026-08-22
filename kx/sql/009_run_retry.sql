BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- A failed run must not block its own retry
--
-- Migration 007 added an attempt counter to `processing_runs` and a trigger that
-- said a terminal status is terminal. Both were right about `succeeded` and the
-- second was too strict about `failed`: a counter that counts attempts only means
-- something if there can be a second attempt.
--
-- Found by an operator error on 2026-08-22 that turned out to be worth having.
-- An extraction pass was started through the wrong wrapper, so the model key was
-- absent and 1053 fragments recorded a failed run each. Because `processing_runs`
-- is unique on (version_id, processor, processor_version, parameters_sha256,
-- model_id), those rows then occupied the key for their own recipe and the
-- fragments could never be processed again - the idempotency that exists to stop
-- successful work being recorded twice had quietly become a way to lose work
-- permanently. A model outage of five minutes would have done the same thing.
--
-- The bounded transition the plan asks for (§10.2) is therefore:
--
--     running  -> succeeded    a result
--     running  -> failed       an attempt that did not produce one
--     failed   -> running      a retry, and the attempt counter must move
--     succeeded -> anything    never
--     failed   -> succeeded    never directly; a retry goes through `running`
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION guard_processing_run_status() RETURNS trigger
LANGUAGE plpgsql
SET search_path = kx, public
AS $$
BEGIN
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'succeeded' THEN
        RAISE EXCEPTION 'processing run % has already succeeded', OLD.run_id;
    END IF;
    IF OLD.status = 'failed' THEN
        IF NEW.status <> 'running' THEN
            RAISE EXCEPTION 'a failed run may only be retried, not set to %', NEW.status;
        END IF;
        IF NEW.attempt_count <= OLD.attempt_count THEN
            RAISE EXCEPTION 'retrying run % must count the attempt', OLD.run_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

UPDATE metadata SET value = '9'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
