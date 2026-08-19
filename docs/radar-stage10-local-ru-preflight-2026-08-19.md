# Radar V2 Stage 10 — Local Ru read-only preflight

Date: 2026-08-19
Status: read-only preflight accepted; remote preparation/deploy not authorized or started
Target: `root@147.45.99.225` (`msk-1-vm-8ymd`)

## Authority boundary

This pass used only read-only SSH/system queries and safe HTTP/TCP probes. It did not create users,
groups, paths or files on Local Ru; install packages/units; transfer an artifact; run a migration;
change permissions, UFW, Caddy or DNS; restart/enable a service; or alter NRD/Radar production.

The next Stage 10 phase is externally mutating and remains blocked until the owner explicitly
approves the exact target objects and commands. The main `radar.aipractice.space` DNS is reserved
for the later cutover stage and is outside this approval boundary.

## Capacity baseline

Captured at `2026-08-19T22:21:01Z` after 17h34m uptime:

- Ubuntu 26.04 LTS, kernel `7.0.0-29-generic`, systemd `259`;
- load average `0.04 / 0.09 / 0.08`; sampled CPU idle `100%`;
- RAM `7.8 GiB` total, `6.4 GiB` available;
- swap `8.0 GiB`, unused;
- root ext4 volume `77 GiB`, `15 GiB` used, `62 GiB` available (`19%` used);
- inode use `3%` (`9,638,332` free).

There is ample capacity for the small Stage 10 application candidate and an empty loopback staging
database. Stage 11 must separately size the full historical seed and retained rollback copies.

## Network, Caddy and firewall baseline

- Caddy `2.11.4` is active+enabled, `NRestarts=0`, configuration valid, about 61 MiB RSS-equivalent
  cgroup memory and 12 tasks.
- The only current Caddy site label is `nrd.aipractice.space`; there is no Radar vhost.
- UFW is active, defaults to deny incoming/allow outgoing. Public allows are SSH 22, Caddy 80/443,
  and Timeweb Zabbix 10050 only from `92.53.116.0/24`.
- NRD ports `3030`, `8787`, `8788`, `18788`, `18792` bind only to `127.0.0.1` and were externally
  closed/filtered from the source host.
- Caddy admin `2019`, PostgreSQL `5432`, main Hermes `8642` and the five NRD application ports are
  loopback-only.
- Candidate Radar API ports `8765`, `8766` and `8767` are currently free; the accepted template
  uses explicit `127.0.0.1:8765`.
- No firewall rule is required for Radar's internal port. A future shadow vhost can use existing
  Caddy 80/443 only after its separate stage/approval.

## NRD non-regression baseline

The canonical current unit names are:

```text
nrd-api.service
nrd-initial-check-worker.service
nrd-analysis-supervisor.service
nrd-hermes-intake.service
nrd-hermes-analysis.service
nrd-hermes-runs-proxy.service
nrd-hermes-analysis-runs-proxy.service
```

All seven are active+enabled with `NRestarts=0`; aggregate sampled cgroup memory was about 808 MiB.
There were zero error-priority journal rows since boot for each unit. Both loopback and public
`/api/health` returned HTTP 200; the loopback body was:

```json
{"status":"ok","api":"ok","worker":"ok"}
```

The obsolete `/healthz` path returns 404 in the final production release and must not be reused as
an NRD health assertion. No NRD process or configuration was changed while reconciling the route
and current unit names.

## Radar target preexistence

The following are all absent on Local Ru, so Stage 10 can fail closed on collision:

- users/groups `radar-v2-api` and `radar-v2-deploy`;
- `/opt/radar-v2-api`;
- `/srv/radar-v2.aipractice.space`;
- `/var/lib/radar-v2`;
- `/etc/radar-v2`;
- all `radar-v2*` systemd units.

No target name is currently shared with NRD.

## Runtime compatibility blocker

The accepted application manifest requires the Stage 1 runtime exactly:

```text
Python: 3.12.3
SQLite: 3.45.1
SQLite source id: 2024-01-30 16:01:20 e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257ccalt1
compile options: 58
compile-options SHA-256: 5583ce88315041f759de8d78dc71c53eaee015bd1d2defc8c347dfa262f47332
```

Local Ru currently provides only:

```text
Python: 3.14.4
SQLite: 3.46.1
SQLite source id: 2024-08-13 09:16:08 c9c2ab54ba1f5f46360f1b4f35d849cd3f080e6fc2b6c60e91b16c63f69aalt1
compile options: 59
compile-options SHA-256: 853d1a8f436d4b4499a2098ddca145816713e53691b7de55d4750be6ea41667f
```

There is no `python3.12`, `uv`, or ordinary Ubuntu 26.04 `python3.12` package candidate on the
target. The migration runner/API correctly reject this host runtime. The system Python and its
SQLite library must not be replaced for Radar.

Recommended resolution: install a dedicated immutable Radar runtime below
`/opt/radar-v2-runtime/releases/<runtime-id>` and point only the Radar unit at it. Before deploy it
must prove Python/SQLite identity, compile-option equality, relocation on Ubuntu 26.04, runtime-file
checksums and rollback. That runtime is a host prerequisite and requires a separately reviewed
package/installation command; it is not silently substituted into the Stage 9 application package.

## Hardened application candidate

The read-only review found and corrected an inert-template mismatch before any installation:
`User=radar-v2` became the contract identity `radar-v2-api`, and the unit gained explicit loopback
bind/allow policy, empty capabilities, strict system/kernel/home/process/namespace protections,
read-only paths and task/memory/file limits. Offline `systemd-analyze security` reports `2.7 OK`.

Clean candidate after the fix:

```text
Commit: 545bf2e11db924b0bacf3b5ac71092495fd8052b
Application release: app_release_20260819_545bf2e
Package: /tmp/radar-stage10-candidate-build-DWq93a/radar-v2-application-release.tar.gz
Package bytes: 69300
Package SHA-256: 85accde8b8c77c1fb8d10e84c267be77e7ca7af8e7fdc7e24e3dfcee02a727eb
Source tree SHA-256: 169db3c2471142d1a8571cd48a101ad4efe0ec854b01d784f49069943a47d180
Migration artifact SHA-256: f3528c6ed3eb05e14c49247700841c4576274fb9491b914b1542ed1e1616c92e
```

The API/web role hashes are unchanged from accepted Stage 9 because only the inert deployment unit
changed. The complete gate remained green: Ruff, strict mypy over 71 files, 142 tests, contracts,
JavaScript, browser smoke, isolation and the 21-file public artifact.

The new clean candidate also passed the retained two-target test-only rehearsal:

```text
Evidence: /tmp/radar-stage10-candidate-evidence-parent-AwQfhe/rehearsal/acceptance.json
Schema SHA-256: 5c7e6e66afc7fd814f25c5bb7b441e22131db8ffc35cf00fd2d81760ccbc6266
rollbackProven: true
```

## Exact next mutation set requiring approval

After an exact-runtime package is reviewed, Stage 10 would be limited to:

1. create system users/groups `radar-v2-api` and `radar-v2-deploy` with no interactive runtime
   login;
2. create the versioned runtime/application and private data/incoming/audit roots documented in
   the master plan;
3. transfer and verify the approved runtime and application package hashes into new quarantine
   paths;
4. install the hardened API unit and root-owned environment file;
5. build an empty schema-only staging content release, migrate source/target copies identically and
   activate only the Local Ru loopback contour;
6. enable/start only `radar-v2-api.service`, prove non-root ownership, `127.0.0.1:8765`, health,
   permissions, hardening score, `NRestarts=0` and unchanged NRD baseline;
7. do not add Caddy/DNS/public activation and do not transfer Legacy production data.

No command from this mutation set has been executed.
