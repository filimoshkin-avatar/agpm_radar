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
- All three mandatory gates completed with exit code 0: V2 verification
  (305 tests, Ruff, mypy, contracts, frontend smokes, design/cache checks,
  isolation, and production artifact), KX verification (575 passed, 202
  expected skips), and KX migration verification (all 777 tests on temporary
  local databases).

Backups, preconditions, and cron evidence are retained under
`/tmp/radar-sep10-fix/`. Full verification logs are
`/tmp/radar-sep10-{v2,kx,migrations}-verify.log`.

## Publication

Recovery uses `RADAR_ISSUE_DATE=2026-09-10` with the existing daily V2 runner and
the already published Legacy JSON. Notification variables are unset for the
manual run. The publisher retains its normal validation, transport, atomic
activation, and comparison steps. The source database and active pointer were
backed up before publication; the backup passes SQLite integrity checking.

Completed at **2026-09-10T07:18:35Z**. The daily runner exited 0.

- Content release: `rel_e84b807ec8f57ffce308dc4f`.
- Candidate: `cand_stage15_daily_20260910_01`.
- Source and production state hash:
  `0b6833c42e3674f337c58d6057af15cf187d11ba3c43f24f6fc6ec360af1a218`.
- Legacy and V2 both contain five materials. Comparison is `matched`, with
  no URL differences and no shared material field differences.
- Daily, 7-day, and 30-day analysis each succeeded on the first attempt using
  `openai/gpt-5.5`; no deterministic fallback or publisher warning occurred.
- Public `/api/latest` and `/api/issues/2026-09-10` return September 10.
  Chromium verified the home page and issue deep link, all five card titles,
  the count, source/public state parity, and no page errors.
- Public title/reference validation, complete source/API issue projection
  equality, SQLite integrity, and foreign-key checks pass.
- The application remains `app_release_20260909_1f45fb2`; content publication
  did not require an application deployment.

The canonical run report is
`/root/.openclaw-projectmanager/workspace/state/radar-v2/dual-run-cron/2026-09-10/combined-report.json`.
Public JSON, browser result, screenshot, and publication log are retained under
`/tmp/radar-sep10-fix/`. Collector fix commit: `c3a6cb2`.
