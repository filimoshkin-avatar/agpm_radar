BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Two rules stage 0b applied wrongly, corrected on the rows it wrote
--
-- Neither is a schema change. Both are rows written by a pass whose rule has
-- since been fixed in code, and a corrected rule that leaves the old rows in
-- place is half a correction: the queues the owner opens are built from these
-- rows, not from the code.
--
-- 1. FRESHNESS WAS MEASURED FROM THE DAY THE PASS RAN.
--
--    `valid_until(kind, dated_on, ...)` fell back to `datetime.now(UTC)` when a
--    statement had no date, and `unread_claims` only ever selected
--    `document_dates.published_on` - so every statement out of a document whose
--    source printed no date arrived with `dated_on = None` and was clocked from
--    the moment of the reading pass.
--
--    6 625 of 13 876 statements on production - every one on a `first_seen`
--    document - carry a `valid_until` exactly one interval after `read_at`, to
--    the day. Their freshness clocks start months late and expire together on
--    the anniversary of a batch job.
--
--    `document_dates.shown_on` is NOT NULL, so there is always a real day to
--    measure from: the day the source published, or the day the radar first saw
--    it. This recomputes every row from the document, which is a no-op for the
--    7 251 already anchored on a published date.
--
-- 2. THE GAP MAP WAS MOSTLY STATEMENTS THE BASE THREW OUT.
--
--    Decision 8's queue answers "what can this base not place?". A statement
--    read as `rejected` is not in the base at all, so what it fails to place is
--    not a gap in the base - it is a property of something the base declined.
--    2 300 of the 2 628 rows were exactly that, and they are the reason the
--    queue reads as noise.
-- ---------------------------------------------------------------------------

--    Two notes on what this deliberately does NOT touch.
--
--    Only rows whose document has no published date are moved. The other 7 251
--    were anchored correctly and are left exactly as they are.
--
--    The interval is counted the way the code counts it, not the way PostgreSQL
--    would. `material_kind_freshness` stores calendar intervals ('3 years'), and
--    psycopg hands Python a `timedelta` of 1 095 days - a year is 365 days and a
--    month 30, with no leap day. So `date + valid_for` in SQL and `dated_on +
--    interval` in Python disagree by a day whenever a leap day falls in the span:
--    4 920 of the correctly-anchored rows sit one day off calendar arithmetic for
--    that reason alone. Rewriting them here would make the rows disagree with the
--    code that maintains them, which is a worse state than being a day out. What
--    "three years" ought to mean is the owner's rule to restate, not a difference
--    to paper over in a repair.
UPDATE claim_reading AS reading
SET valid_until = (
        (dates.shown_on::timestamp AT TIME ZONE 'UTC')
        + (
            EXTRACT(YEAR FROM freshness.valid_for) * 365
            + EXTRACT(MONTH FROM freshness.valid_for) * 30
            + EXTRACT(DAY FROM freshness.valid_for)
          ) * interval '1 day'
    )
FROM claims,
     document_versions AS versions,
     document_dates AS dates,
     material_kind_freshness AS freshness
WHERE claims.claim_id = reading.claim_id
  AND versions.version_id = claims.version_id
  AND dates.document_id = versions.document_id
  AND freshness.material_kind = reading.material_kind
  AND freshness.valid_for IS NOT NULL
  AND dates.published_on IS NULL;

-- A statement whose document carries no date at all cannot have its freshness
-- measured, and saying so is the honest answer. None exist today - every one of
-- the 8 354 documents was dated in stage 0a - so this is here to keep the rows
-- and the rule saying the same thing if one ever does.
UPDATE claim_reading AS reading
SET valid_until = NULL
WHERE reading.valid_until IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM claims
      JOIN document_versions AS versions ON versions.version_id = claims.version_id
      JOIN document_dates AS dates ON dates.document_id = versions.document_id
      WHERE claims.claim_id = reading.claim_id
  );

DELETE FROM claim_gaps AS gaps
USING claim_reading AS reading
WHERE reading.claim_id = gaps.claim_id
  AND reading.admission = 'rejected';

UPDATE metadata SET value = '27'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
