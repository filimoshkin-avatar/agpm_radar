# Radar V2 Stage 15 — Project Manager daily dual-run

Date: 2026-08-20

Status: complete for the owner-approved 20 August acceptance target.

## Scope and result

Stage 15 preserved the existing 08:00 Europe/Moscow Legacy Project Manager job and added a
separate post-Legacy V2 command job at 08:25. The separation is the timeout/failure boundary:
Legacy collection, DOCX delivery and Legacy publication complete independently before V2 takes an
immutable snapshot, creates disjoint Legacy/V2 forks, publishes when the date is absent, and emits
a combined comparison report.

The active Project Manager jobs are:

- Legacy `cfa00d0b-ae82-4e88-9119-6d1844a6a728`, unchanged schedule and payload;
- V2 `0af53bc1-2fea-4b66-bce6-b5d3a8e4f064`, enabled at `25 8 * * *`,
  `Europe/Moscow`, exact argv `/mnt/vdd/Radar/v2/scripts/run_stage15_daily.sh`.

The three pre-existing Legacy working-tree changes remain untouched and outside the Stage 15
commit:

- `pipeline/scripts/agpm_radar_collect.py`;
- `pipeline/scripts/agpm_radar_daily.sh`;
- `pipeline/scripts/agpm_radar_report.py`.

## Backup and rollback

Before the cron mutation, a consistent backup was retained at
`/root/.openclaw-projectmanager/backups/radar-stage15-before-20260820T1048Z`:

- `openclaw.json`;
- `agpm-radar.env`;
- SQLite online backup of the active OpenClaw state store, `integrity_check=ok`;
- readable exact export of Legacy cron row/payload/state.

The pre-change Legacy job is therefore recoverable without using the migrated `jobs.json` files.
Rollback of Stage 15 is disabling the new V2 cron row; the Legacy row requires no restoration
because it was not edited. No file, release, failed run or backup was deleted.

## Daily runner boundary

`tools/run_stage15_dual.py` validates the requested Legacy public issue, creates one immutable
snapshot, forks it into disjoint Legacy and V2 workspaces with matching attestations, and checks
the current V2 source state. If the date is absent, it builds the closed daily candidate and uses
the Stage 14 restricted publisher. If the date is already present, it returns
`already_published` and does not move either pointer. In both cases it reads the shadow V2 public
endpoint and creates a canonical combined comparison.

`scripts/run_stage15_daily.sh` supplies only fixed allowlisted paths and the Moscow issue date. It
prints a bounded owner-facing summary suitable for cron delivery. The cron command has a 900
second timeout and does not run the Legacy collector.

## 20 August scheduler acceptance

The first manual scheduler run failed before V2 execution because the command job did not inherit
the repository working directory. The failure was retained in cron history, its failure
notification was delivered, and Legacy/source/Local Ru state was unchanged. The wrapper now uses
an explicit `/mnt/vdd/Radar/v2` working directory.

The second run, executed through the active Project Manager gateway rather than by calling the
wrapper directly, completed successfully:

- cron run id
  `manual:0af53bc1-2fea-4b66-bce6-b5d3a8e4f064:1787223288006:2`;
- status `ok`, duration 287 ms;
- Telegram delivery `delivered`;
- Legacy: 10 materials, LLM `success`;
- V2: 9 materials, LLM `success`;
- only-Legacy: one URL, the material deliberately excluded by the accepted V2 30-day rule;
- only-V2: zero;
- publication disposition `already_published`, proving the cron did not duplicate the Stage 14
  publication of the same date;
- active release `rel_5753b19670d1ed8d3cf539fa`;
- active logical state
  `0a4517d792d64e63633872b31337cd22bc858ae4f128d580c66df8ea0318f5ff`.

The canonical comparison is retained at
`/root/.openclaw-projectmanager/workspace/state/radar-v2/dual-run-cron/2026-08-20/combined-report.json`.
The 20 August release is therefore covered by the new two-cron Project Manager contour: Legacy
assembled and delivered the source issue, while the new post-Legacy job independently consumed
the same date, proved the snapshot/fork boundary, verified V2 publication and delivered the
combined result. It intentionally did not create a second daily release for an already-published
date.

## Verification

- Ruff format/lint: PASS (84 source files);
- strict mypy: PASS (84 source files);
- pytest: PASS (155 tests);
- frozen contracts: 6 schemas, 8 examples, 23 tables and 11 public paths PASS;
- JavaScript and Legacy-parity frontend console smoke: PASS;
- isolation/secret scan: PASS (109 files, three synthetic fixtures);
- deterministic production artifact: 26 files, SHA-256
  `9cb0f5e96cbebc0513737a13f7ced04fab69644a31d75964b2c24e68594306bb`;
- `git diff --check`: PASS.

## GRACE delta and remaining boundary

GRACE-Delta: skip — Radar has no GRACE module metadata. The Stage 15 runner, cron boundary,
backup, failure isolation, scheduler evidence, comparison and rollback are recorded here.

Stage 16 reboot/disaster-recovery rehearsal and later cutover stages are not authorized by this
acceptance and were not started.
