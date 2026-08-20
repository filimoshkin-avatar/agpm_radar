# Radar V2 Stage 14 — Project Manager end-to-end manual dry runs

Date: 2026-08-20

Status: complete.

## Scope and boundary

Stage 14 connected the Project Manager source state to the Stage 13 restricted Local Ru
activator and exercised the approved manual flows. No V2 cron was enabled. The existing Project
Manager Radar cron, Legacy production, DNS/Caddy/systemd units and Stage 15 dual-run boundary were
not changed.

The three pre-existing dirty Legacy pipeline files were preserved and are still the only Legacy
working-tree changes:

- `pipeline/scripts/agpm_radar_collect.py`;
- `pipeline/scripts/agpm_radar_daily.sh`;
- `pipeline/scripts/agpm_radar_report.py`.

## Implemented publication boundary

The source-side runner now verifies one immutable candidate package, finalizes the source release,
builds a closed typed delta, invokes only the Stage 13 forced-command SSH transport, verifies the
exact remote result, installs the source release and atomically commits the source pointer. Machine
result and owner-facing Project Manager report are create-only. A completed idempotency key is
replayed locally without a second transport request.

Gazette candidates carry descriptor-bound exact asset bytes as canonical lowercase hex inside the
bounded closed request. The remote activator verifies membership, byte count and SHA-256, installs
only `gazettes/*` paths under private `radar-v2-api` ownership and activates the database pointer
only after the immutable asset is present.

Final activator release on Local Ru:
`/opt/radar-v2-activator/releases/stage14-final-8c9a4b1`.

All prior activator releases and failed/partial Stage 14 evidence remain retained. Nothing was
deleted.

## Accepted manual scenarios

1. Daily publication
   - candidate `cand_stage14_daily_20260820_02`;
   - release `rel_648b2f0d0f46d78eedc370ba`;
   - nine eligible real materials published from the captured Legacy response;
   - one 2024 material was explicitly excluded by the V2 30-day gate;
   - retry returned `already_succeeded` / `replayed`, generated no second SSH request and did not
     move either pointer.
2. Explicit no-LLM
   - candidate `cand_stage14_no_llm_20260819_03`;
   - release `rel_d6e524a92f04db2a82e1dea7`;
   - `PROVIDER_UNAVAILABLE` and deterministic `rules-daily@1` fallback are explicit in the
     accepted candidate and final Project Manager report.
3. Historical duplicate correction
   - accepted target date `2026-06-09`;
   - removed two confirmed duplicate URLs whose earlier accepted instances remain in
     `2026-06-08`;
   - material count changed from 12 to 10;
   - repair candidate `cand_stage14_historical_dedup_repair_20260609_01` corrected the discovered
     legacy future-date quality classification; the final public endpoint is HTTP 200.
4. Gazette update and asset transport
   - asset repair candidate `cand_stage14_gazette_asset_repair_202608_01` and final canonical-hex
     transport proof `cand_stage14_gazette_hex_transport_proof_202608_01`;
   - final release `rel_5753b19670d1ed8d3cf539fa`;
   - `/gazettes/2026-08/` returns 36,237 bytes with SHA-256
     `1e6ba2bb055a2821bca2e05ad7ef6ec57e3a558049875ffc5e601c58911b637d`, exact to the accepted
     HTML asset.

## Fail-closed findings and fixes

- correction removal now deletes every `material_rubrics` row with its complete three-column key;
- future-dated Legacy anomalies retain the required queued severity and no longer make the public
  DTO fail closed after correction;
- the restricted transport now carries exact gazette bytes rather than metadata alone;
- asset parents/files are exact `radar-v2-api:radar-v2-api` `0700`/`0600` and single-link.

Rejected candidates, failed staging roots and the initial metadata-only gazette release were kept
as audit evidence. The final accepted releases supersede them in the append-only ledger.

## Final state and acceptance

- source and Local Ru active release: `rel_5753b19670d1ed8d3cf539fa`, sequence 8;
- logical state: `0a4517d792d64e63633872b31337cd22bc858ae4f128d580c66df8ea0318f5ff`;
- exact source and Local Ru pointer SHA-256:
  `c33f5daad0c4d156d3497fbe51de5f02ef57ac9efe13d404170894876d407fc6`;
- source physical DB SHA-256:
  `1e7c0eefece21e7e088f3930ca50154335389f749905cee2920e1e62899fe7cb`;
- Local Ru physical DB SHA-256:
  `b408849e5729e4e84c269e03f5f059687e09483594230077467bf7340f2ef6d5`;
- physical hashes differ as expected for independently applied SQLite transactions;
- public health, issues `2026-06-09`, `2026-08-19`, `2026-08-20`, gazette list and gazette asset:
  HTTP 200;
- Radar API PID `37933`, `NRestarts=0`;
- Legacy DB SHA-256 remains
  `405b1c382dea770aa5631323b7f339860abfe8ca6c769db8f80b0a4e76c412b3`;
- Project Manager `openclaw.json`, `agpm-radar.env` and the complete Radar cron row are byte/config
  unchanged; cron remains enabled in its original Legacy configuration.

Evidence root: `/root/radar-stage14-evidence-20260820T1010Z`.

Consistent pre-stage Project Manager backup:
`/root/.openclaw-projectmanager/backups/radar-stage14-before-20260820T100934Z`.

## Verification

- Ruff format/lint: PASS (82 source files);
- strict mypy: PASS (82 source files);
- pytest: PASS (154 tests);
- frozen contracts: 6 schemas, 8 examples, 23 tables and 11 public paths PASS;
- JavaScript and Legacy-parity frontend console smoke: PASS;
- isolation/secret scan: PASS (106 files, three synthetic fixtures);
- deterministic production artifact: 26 files, SHA-256
  `9cb0f5e96cbebc0513737a13f7ced04fab69644a31d75964b2c24e68594306bb`;
- `git diff --check`: PASS.

## GRACE delta and next boundary

GRACE-Delta: skip — Radar has no GRACE module metadata. The Stage 14 changes, invariants,
regressions, runtime release, live evidence and remaining boundary are recorded here.

Stage 15 dual-run and the Project Manager cron transition were explicitly approved by Ivan in
Telegram message `24487`; Stage 15 starts immediately after this acceptance commit. Production
cutover still requires the Stage 15 gates and rollback proof described by the migration plan.
