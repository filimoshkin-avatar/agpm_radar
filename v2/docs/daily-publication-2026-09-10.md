# September 10 daily publication recovery

## Evidence and cause

The Legacy cron session at 05:01:31 UTC explicitly records collection stopping
on `https://incidentdatabase.ai/rss.xml`, `TITLE_QUALITY_GATE`, and `No title`.
The Project Manager cron database records the Legacy job at 05:00 UTC as
successful after 431,477 ms. Its collector runtime file was changed at
05:01:56 UTC, adding a `TitleQualityError` catch that skips an invalid candidate.
That change was absent from `pipeline/scripts/agpm_radar_collect.py`.
The corpus contains the AI Incident Database RSS record `b5d88f57ebc7377e`
with title `No title` and URL `https://incidentdatabase.ai/rss.xml`.

The independent V2 job started at 05:25 UTC and failed after 96 ms with
`Legacy runtime mirror drift: agpm_radar_collect.py`. No September 10 candidate,
run directory, or source issue existed. The source pointer still referenced
`rel_38e987359567b2993d5895ba`; the public September 10 issue endpoint returned
HTTP 404. Legacy's exported issue contained five materials.

## Correction

Collection now rejects an unrecoverable candidate individually, before identity,
classification, or persistence. It catches only `TitleQualityError`; unrelated
failures still propagate. The same isolation covers an existing stored invalid
title encountered on rediscovery, without mutating that record on failure.

`TITLE_QUALITY_REJECTED` diagnostics include the existing bounded, escaped
`TITLE_QUALITY_GATE` reason, URL, and value. They are emitted to stderr and passed
to the persistent collection run log through its technical notes. The normal
stdout result counts them in `note_count`.

This narrows the collection failure policy documented in
`title-quality-2026-09-09.md`: a rejected discovery no longer stops unrelated
discoveries. Report rendering and independent V2 candidate/public title gates
remain strict. The four-file runtime mirror guard remains enabled.

Both repository and runtime collector copies were installed from the same
reviewed content, SHA-256
`d811d3f3e4768822ef7355b43c7122290b80585cc5bcb912ef36e51315f62522`.
Installation verified all pre-change hashes and retained backups first.
The daily shell script and scheduler configuration were not changed.

## Verification

- Twelve Legacy title tests pass, including mixed valid/invalid input, persisted
  rejection diagnostics, stored-record immutability, and unexpected failures.
- Fifty V2 title tests pass, including the Legacy subprocess suite and strict
  report/candidate/publication rejection checks.
- Independent review found no blockers or material risks.
- Four-file Legacy runtime mirror check passes.

Backups, preconditions, and cron evidence are retained under
`/tmp/radar-sep10-fix/`. Full verification logs are
`/tmp/radar-sep10-{v2,kx,migrations}-verify.log`.

## Publication

Recovery uses `RADAR_ISSUE_DATE=2026-09-10` with the existing daily V2 runner and
the already published Legacy JSON. Notification variables are unset for the
manual run. The publisher retains its normal validation, transport, atomic
activation, and comparison steps. The source database and active pointer were
backed up before publication; the backup passes SQLite integrity checking.
