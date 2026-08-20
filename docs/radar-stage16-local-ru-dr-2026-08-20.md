# Radar V2 Stage 16 — Local Ru reboot and disaster-recovery rehearsal

Date: 2026-08-20

Status: complete. This is the final implementation stage in the owner-agreed migration boundary;
Stages 17–19 are not part of the current plan. Legacy, DNS and cutover were not changed.

## Pre-Stage 16 correction

The Stage 15 comparison exposed one expected content difference and one V2 consistency defect.
Legacy included a material published on 2024-10-29, while V2 correctly excluded it under the
30-day eligibility rule. The V2 issue still inherited Legacy prose claiming 10 materials and a
non-zero middle perimeter even though its cards and statistics contained 9 materials and no
middle-perimeter material.

Commit `2f3f5de` reconciles issue prose after eligibility filtering, supports the same repair for an
accepted correction, and makes the correction builder compatible with both imported object-shaped
flags and native V2 list-shaped flags. The full V2 gate passed with 158 tests.

Correction `cand_v2_20260820_narrative_reconcile_02` was published through the restricted forced
command. Source and Local Ru converged on:

- release `rel_e5170e7d1a7b23ecb8a68fe4`, sequence 9;
- logical state `3889686b0f6e055ebf6520d008a5dcfb051d5ff903c44330e3eb2f40f3523711`;
- exact pointer SHA-256 `52451e5cb8ce917e0e3d20485cad1e649c8a3e3dc3282161723dcef3b7177f09`.

The public issue now reports 9 materials, near/mid/far `4/0/5`, and contains no stale 10-material
or middle-perimeter claim.

## Backups and reboot

Before reboot, private backups were retained at:

- source/Project Manager: `/root/.openclaw-projectmanager/backups/radar-stage16-before-20260820T1142Z`;
- Local Ru: `/root/radar-stage16-before-20260820T1142Z`.

Both contain the active pointer, an online SQLite backup with `integrity_check=ok`, configuration
and immutable target metadata. The Local Ru backup also contains Caddy, Radar environment/unit,
tmpfiles and application/runtime link targets. Nothing was deleted.

The approved Local Ru reboot changed boot ID from `a1c3b6ec3d124a82999093e406518872` to
`3d56678d-4505-4721-b92f-3b5bdb21ebc5`. After boot:

- system state was `running` and no units were failed;
- Radar API, Caddy and all seven NRD units were enabled and active;
- all nine checked units had `NRestarts=0`;
- Radar loopback/public health retained release/state and the corrected issue;
- NRD public health returned API and worker `ok`;
- tmpfiles ACLs were restored;
- boot error and checked service warning journals were empty;
- Radar and NRD application ports remained loopback-only.

## Application rollback

The application pointer was atomically changed from `app_release_20260820_10fc9c8` to the retained
compatible `app_release_20260820_21b111a`. After the service restart, loopback Radar, public Radar
and public NRD health were green. Systemd journal timestamps show the rollback service start at
11:33:50.574076 UTC and the restore start at 11:33:51.929876 UTC, a 1.356-second bounded rehearsal
interval. The final pointer was restored to `app_release_20260820_10fc9c8`; the service is active
with `NRestarts=0`.

## Database release rollback

The restricted rollback action temporarily moved Local Ru from sequence 9 to the retained
sequence-8 release `rel_5753b19670d1ed8d3cf539fa`, state
`0a4517d792d64e63633872b31337cd22bc858ae4f128d580c66df8ea0318f5ff`.

- rollback operation: 2,137 ms; public convergence: 149 ms;
- restore operation: 1,542 ms; public convergence: 119 ms;
- loopback verification passed in both directions;
- final active pointer was restored exactly to sequence 9 SHA-256 `52451e5c...7f09`.

The source pointer was not moved during this rehearsal.

## Full-seed recovery and reconciliation

A fresh full seed was exported from the accepted sequence-9 source release and transferred to a
private Local Ru evidence directory. Recovery uses separate deployment tooling rather than adding
mutation code to the minimal public API artifact. The recovery command must load
`/etc/radar-v2/api.env`; without it the immutable Python binary resolves system SQLite 3.46.1,
while the accepted runtime contract is SQLite 3.45.1. The strict runtime gate rejected that failed
attempt before target creation, and the failed evidence was retained.

The successful create-only import used the accepted runtime environment and exact source tooling:

- restore time: 708 ms;
- `integrity_check=ok`;
- 5,840,896 bytes, SHA-256
  `fbf06e295e47c9d377b85b6e5c5d7a9843cd8ac20edfd1131fd07bbe7e9b6f20`;
- 23 replicated tables;
- release/sequence/state equal to accepted sequence 9;
- corrected 20 August public aggregate contains 9 materials.

The canonical reconciliation compared release metadata plus every table count and logical hash:

- source equals active production: true;
- source equals recovered seed: true.

Evidence is retained on source at `/root/radar-stage16-evidence-20260820T1142Z` and on Local Ru at
the same path. Failed/partial attempts remain retained; no backup, release or migration artifact
was removed.

## Final state and observation

Legacy `https://radar.aipractice.space` and V2 `https://radar.agpm.space` both return HTTP 200.
The independent Legacy cron was not modified. The Stage 15 V2 cron
`0af53bc1-2fea-4b66-bce6-b5d3a8e4f064` remains enabled at 08:25 Europe/Moscow with last status
`ok`, successful Telegram delivery and zero consecutive errors. Its next absent-date publication
is an observation event, not an additional implementation stage under the owner's final boundary.

GRACE-Delta: skip — Radar has no GRACE module metadata. The defect, regression coverage,
publication, backups, reboot, rollback timings, recovery requirements, reconciliation and final
health are recorded here.
