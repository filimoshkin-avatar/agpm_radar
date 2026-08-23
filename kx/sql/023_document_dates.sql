BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- When a document was published, and what to show when nobody knows (stage 0a)
--
-- The store knows when the radar *saw* something - `documents.first_seen_at` and
-- `document_versions.fetched_at` are both recorded - and it keeps whatever the
-- source said about publication as text, in `source_materials.published_raw` and
-- `issue_perimeter_members.published_raw`. What it has never had is a **date**:
-- nothing on the document, nothing on the version. So "show me every incident
-- this quarter" could not be answered, and `claim_reading.valid_until` had no
-- day to count its interval from.
--
-- The owner's rule for the observatory (plan of 2026-08-23, stage 0a) is the
-- shape of this table: lean on the publication date where there is one, on the
-- date the radar found it where there is not, **and say which of the two is on
-- screen**. A chronicle that silently mixes the two is a chronicle that dates
-- an article to the day somebody happened to crawl it.
--
-- Recomputation, not decision: every row here is derived from text that is
-- already stored, so a better parser re-running leaves one row per document
-- rather than a second opinion. Nothing is appended and nothing is anybody's
-- judgement, which is why this table may be rewritten in place while
-- `issue_perimeter_members` beside it may not.
-- ---------------------------------------------------------------------------

CREATE TABLE document_dates (
    document_id char(64) PRIMARY KEY REFERENCES documents(document_id),

    -- What the source said, verbatim, kept so a wrong reading can be traced back
    -- to what it was reading. NULL when the source said nothing at all.
    published_raw text,

    -- Where that text came from. The issue perimeter is the radar's own record
    -- of the material and wins over the corpus row, which is older and coarser.
    raw_source text NOT NULL CHECK (
        raw_source IN ('issue_perimeter', 'source_material', 'none')
    ),

    -- What the text parsed to, and how much of it the source actually gave. A
    -- month is stored as its first day and a year as its first of January - but
    -- `date_precision` is what stops that convention being read as a real day.
    published_on date,
    date_precision text NOT NULL CHECK (
        date_precision IN ('day', 'month', 'year', 'none')
    ),

    -- What the observatory puts on screen, and which of the two dates it is.
    -- Never NULL: `document_versions.fetched_at` is NOT NULL, so every document
    -- in the store has at least the day the radar reached it.
    shown_on date NOT NULL,
    shown_kind text NOT NULL CHECK (shown_kind IN ('published', 'first_seen')),

    resolved_at timestamptz NOT NULL DEFAULT clock_timestamp(),

    -- The two halves have to agree: if a publication date is shown, one was
    -- parsed; if none was parsed, what is shown is the radar's own date.
    CONSTRAINT what_is_shown_is_what_was_found CHECK (
        (shown_kind = 'published' AND published_on IS NOT NULL AND shown_on = published_on)
        OR (shown_kind = 'first_seen' AND date_precision = 'none')
    ),
    CONSTRAINT a_parsed_date_names_its_precision CHECK (
        (published_on IS NULL) = (date_precision = 'none')
    ),
    CONSTRAINT a_parsed_date_came_from_somewhere CHECK (
        date_precision = 'none' OR raw_source <> 'none'
    )
);

-- The observatory's own query: everything in a period, newest first.
CREATE INDEX document_dates_shown_idx ON document_dates (shown_on DESC, shown_kind);

-- "Which documents are only dated by the crawl" has to be one scan, because it
-- is the caveat the reader is shown and the queue somebody may want to work.
CREATE INDEX document_dates_undated_idx ON document_dates (document_id)
    WHERE shown_kind = 'first_seen';

GRANT ALL ON document_dates TO radar_kx;

UPDATE metadata SET value = '23'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;
