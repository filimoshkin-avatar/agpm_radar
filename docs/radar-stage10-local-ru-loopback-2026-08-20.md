# Radar V2 Stage 10 — Local Ru loopback contour

Date: 2026-08-20
Status: accepted; loopback-only schema contour active, no public activation or Legacy seed
Target: `root@147.45.99.225` (`msk-1-vm-8ymd`)

## Authority and boundary

The owner explicitly continued the exact mutation set documented in
`radar-stage10-local-ru-preflight-2026-08-19.md`: install a separate exact runtime, create the two
Radar identities and new paths, transfer the approved application candidate, install the hardened
API unit, create only an empty schema release, and start only a loopback service. Caddy, UFW, DNS,
Legacy data, cron and publisher transport remained outside this authority.

Stage 10 therefore does **not** make Radar V2 public and does not claim historical acceptance. The
main hostname still resolves to the Legacy server. Stage 11 remains the separately approved full
seed and historical-parity boundary. The `radar-v2-deploy` identity and private incoming/audit
roots are prepared, but no SSH key, forced command or activator transport was installed; that
remains part of the separately tested Stage 13 publisher integration.

## Accepted artifacts

Application candidate:

```text
commit: 545bf2e11db924b0bacf3b5ac71092495fd8052b
application release: app_release_20260819_545bf2e
package bytes: 69300
package SHA-256: 85accde8b8c77c1fb8d10e84c267be77e7ca7af8e7fdc7e24e3dfcee02a727eb
API role SHA-256: c807e9208aa811a0bb47b3341ebf4a4f4f4ff7dd628f7911cc62e03d6680c0e3
migration role SHA-256: f3528c6ed3eb05e14c49247700841c4576274fb9491b914b1542ed1e1616c92e
web role SHA-256: d96e5e30346d641bc5ee8d672b6ef2380f875d189b28c86454321c5669ff65d2
```

Dedicated runtime:

```text
runtime ID: cpython-3.12.3-sqlite-3.45.1-5d4c1b2f839a
archive bytes: 25181236
archive SHA-256: 8f6045cc98c8792b0ed68816a10ffa560d8e964b42d7649d25fe25a455a85c88
payload SHA-256: 5d4c1b2f839aa038c4dd936913053f69137601455028e8134061d3981cc210ef
Python: 3.12.3
SQLite: 3.45.1
SQLite source ID: 2024-01-30 16:01:20 e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257ccalt1
compile options: 58
compile-options SHA-256: 5583ce88315041f759de8d78dc71c53eaee015bd1d2defc8c347dfa262f47332
```

The runtime was built twice into separate local roots; both archives were byte-identical. The
archive contains one normalized root, 748 members, 746 declared payload entries, root ownership,
normalized modes/timestamps, only regular files/directories/internal symlinks, and 34 bundled
non-glibc shared-library dependencies. Independent relocation on Ubuntu 26.04 proved the exact
Python/SQLite identity and that `_sqlite3` loads the bundled SQLite library rather than the host
SQLite 3.46.1.

## Installed identities and layout

Locked noninteractive identities:

```text
radar-v2-api     uid=994 gid=976 home=/nonexistent shell=/usr/sbin/nologin
radar-v2-deploy  uid=993 gid=975 home=/nonexistent shell=/usr/sbin/nologin
```

Current immutable targets:

```text
/opt/radar-v2-runtime/current
  -> releases/cpython-3.12.3-sqlite-3.45.1-5d4c1b2f839a-reinstall-20260820T005200Z
/opt/radar-v2-api/current
  -> releases/app_release_20260819_545bf2e
/srv/radar-v2.aipractice.space/current
  -> releases/app_release_20260819_545bf2e
```

The API role has 18 manifest files, web has 3, and the separately retained migration role has 21.
Every installed byte, file count, mode and single-link invariant matches its role manifest. The API
tree is root:`radar-v2-api` `0550/0440`; the web tree is root:root `0555/0444`; migration input is
root:`radar-v2-deploy` `0550/0440`. The exact outer package and checksums remain in private incoming
quarantine.

Data/config permissions:

```text
/var/lib/radar-v2                         root:root            0755
/var/lib/radar-v2/content                 root:radar-v2-api    0750
/var/lib/radar-v2/incoming                root:radar-v2-deploy 0750
/var/lib/radar-v2/audit                   root:radar-v2-deploy 0750
/var/lib/radar-v2/backups                 root:root            0700
/etc/radar-v2                             root:radar-v2-api    0750
/etc/radar-v2/api.env                     root:radar-v2-api    0440
/etc/systemd/system/radar-v2-api.service  root:root            0644
```

`/var/lib/radar-v2` is readable/traversable only at the directory-name level because the no-follow
walker opens every parent read-only. All data-bearing children retain their narrower modes.

## Empty schema release

Stage 10 intentionally activated no Legacy rows. A newly created empty source database and an
independent Local Ru staging copy both applied the exact candidate migration bundle (`0001`,
`0002`) with the exact runtime. The migrated files are byte-identical:

```text
content release: content_release_stage10_empty
database SHA-256: 84d265ab774ffafd9b7922adf1f26297802c3dd2ee719d5c7ba7147af03308b6
schema SHA-256: 5c7e6e66afc7fd814f25c5bb7b441e22131db8ffc35cf00fd2d81760ccbc6266
logical state SHA-256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
schema migrations: 2
application compatibility rows: 2
content release rows: 1
issues/gazettes and all other domain rows: 0
```

Both copies passed integrity, foreign keys, schema/compatibility, FTS parity, full table inventory,
counts and hashes. The active pointer and database are `0600` and owned by `radar-v2-api`. Inside
the service mount namespace, non-writing `open(O_WRONLY|O_NOFOLLOW)` probes fail with `EROFS` for
both files, and their SHA-256 values remain unchanged.

## Service and API acceptance

Installed config hashes:

```text
api.env SHA-256: 169378adb1fd79cdb2a35818a8cb0f2cb17fcc15e43cbfb69a4c6370ec3e5b16
unit SHA-256: c13b324a27008ee579be435de9ae6646eee74356c59ff56065f7fe081d853dff
```

Final process state:

```text
radar-v2-api.service: active, enabled, NRestarts=0
runtime identity: radar-v2-api:radar-v2-api (non-root)
listener: 127.0.0.1:8765 only
capability inherited/permitted/effective/bounding/ambient sets: all zero
NoNewPrivileges: 1
seccomp mode: 2, filters: 30
MemoryMax: 512 MiB
TasksMax: 128
systemd-analyze security: 2.7 OK
error-priority journal rows: 0
```

The exact loopback health marker is:

```json
{"databaseStateHash":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","releaseId":"content_release_stage10_empty","schemaVersion":1,"status":"ok"}
```

A 14-request acceptance matrix proved health, empty issue/material/search/gazette lists, zero stats,
empty time series/rubrics/sources, expected latest 404, hidden internal endpoint 404, malformed-query
400, write-method 405, root 404 and the exact CSP/cache/referrer/permissions/content-type security
headers. Ports `8765`, `8766` and `8767` are closed/filtered from the source server. There is no
Radar timer or other installed `radar-v2*` unit.

The stdlib transport still emits a loopback-only `Server: BaseHTTP/0.6 Python/3.12.3` header. It is
not publicly reachable in Stage 10; it must be suppressed in the application or stripped and tested
at the Stage 12 Caddy shadow boundary before any public hostname is accepted.

## Runtime membership incident and remediation

Final artifact acceptance found 82 extra root-owned `__pycache__` directories/files in the first
installed runtime. Their timestamps proved they came from two acceptance probes that executed the
exact runtime as root without `PYTHONDONTWRITEBYTECODE=1`; the API user could not create them. All
declared members/hashes remained intact, but immutable membership correctly failed closed.

No file was deleted. The drifted release and the complete 82-entry diff were retained. The original
quarantine archive was independently revalidated, extracted into a new create-only release, checked
with the host system Python, and atomically activated. The previous target remains reachable through
`current.before-reinstall-20260820T005200Z`. Only `radar-v2-api.service` was restarted. After service
and API-user probes the new target still has exactly 747 members (manifest plus 746 payload entries),
all modes/hashes match, the running executable resolves into the new target, health is green and
`NRestarts=0`. Future root validation uses the system Python or explicitly disables bytecode writes.

## NRD, Caddy, firewall, DNS and Legacy non-regression

- All seven canonical NRD services remain active+enabled with `NRestarts=0`; error-priority journals
  are empty and loopback/public `/api/health` return `{"status":"ok","api":"ok","worker":"ok"}`.
- NRD ports `3030`, `8787`, `8788`, `18788`, `18792` and Radar `8765` each bind exactly once to
  `127.0.0.1`. Radar `8765`-`8767` remain externally closed/filtered.
- Caddy remains active+enabled with `NRestarts=0`; its config SHA-256 is
  `152ae52f1daf863f8d04d9843599f682dba948e202ebd11b8a14d7460243e24d`, validates successfully and
  contains no Radar vhost. UFW remains active with deny incoming/allow outgoing defaults.
- Cloudflare, Google, Quad9 and the system resolver all return `72.56.107.196` for
  `radar.aipractice.space`; DNS was not changed.
- Legacy `data/db/radar.sqlite` remains byte-identical at
  `481d5d6c9b54a58f78f288fb29c0eb072d43e74d6c2db8b14044a3153cd8f7f7`.
- Legacy `radar-api.service` and source Caddy remain active with `NRestarts=0`; the official
  production healthcheck passes for issue `2026-08-19` with three materials.

No package was installed into the host system Python, no system Python/library was replaced, and
no Caddy, UFW, DNS, cron, Legacy database or NRD service/configuration was changed.

## Retained evidence

```text
Local Ru quarantine: /root/radar-stage10-quarantine-20260820T001220Z
Local Ru evidence:   /root/radar-stage10-evidence-20260820T001220Z
Local exact build A: /tmp/radar-stage10-runtime-build-muz2r611
Local exact build B: /tmp/radar-stage10-runtime-build-lb5toep_
Local empty DB:      /tmp/radar-stage10-empty-source-20260820T001220Z
Legacy healthcheck:  /tmp/radar-stage10-legacy-health-pwEElQ
```

The evidence root contains account-file backups, exact package/config hashes, local/remote migration
acceptance, API matrix, process/security/listener state, systemd score, journals, Caddy/UFW/NRD
non-regression, runtime drift inventory, secure archive/tree verifier, pre/post runtime targets and
final exact-tree acceptance. Failed staging/runtime artifacts were retained; nothing was cleaned up.

## Mandatory repository gate

Final verification from the repository root after all Stage 10 records were updated:

```text
Ruff format: 71 files already formatted
Ruff lint: PASS
strict mypy: 71 source files PASS
pytest: 142 passed
contracts: 6 JSON schemas, 8 examples, 23 SQLite tables, 11 public API paths PASS
JavaScript syntax: PASS
frontend console smoke: PASS (empty/no-LLM route)
secret/Legacy isolation: 87 files, 3 synthetic fixtures PASS
public production artifact: 21 runtime files
public artifact SHA-256: 07bdfd832ad88e7618db5b2fc1df64830f7bf32625db27d0d91c16feacdbf572
```

## Next boundary

Stage 11 must separately approve transfer of the verified full Legacy-derived V2 source database
and historical metadata to Local Ru. It must compare every replicated table count/hash, prove
draft/queue physical presence and public invisibility, exercise historical correction/rollback, and
perform endpoint parity. Stage 10 authority does not permit that data transfer, public Caddy/DNS,
publisher SSH integration or cron activation.
