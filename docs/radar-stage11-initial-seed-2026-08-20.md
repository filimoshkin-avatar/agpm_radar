# Radar V2 Stage 11: initial seed и историческая acceptance

Дата выполнения: 2026-08-20

Целевой сервер: Local Ru, `147.45.99.225`

Статус: **завершено**

## 1. Граница полномочий

Владелец явно разрешил Stage 11 после завершения Stage 10. В разрешённую область вошли:

1. детерминированная сборка V2 SQLite из frozen Legacy inputs;
2. передача и create-only установка full seed на Local Ru;
3. активация seed только в существующем loopback-контуре;
4. полная table/hash acceptance, historical API parity и public-invisibility checks;
5. disposable historical correction и content-pointer rollback/re-activation.

Stage 11 не разрешал и не выполнял:

- добавление Caddy vhost или shadow hostname;
- изменение DNS;
- установку publisher SSH transport/forced command;
- включение cron или dual-run;
- reboot;
- public cutover;
- удаление Stage 10 release, Legacy данных или диагностических артефактов.

## 2. Исходное состояние

До Stage 11 на Local Ru был активен пустой Stage 10 release
`content_release_stage10_empty`; `radar-v2-api.service` работал non-root на
`127.0.0.1:8765`, был active+enabled, имел PID `23919` и `NRestarts=0`.

Local Ru не содержал Radar vhost. `radar.aipractice.space` продолжал указывать на Legacy IP
`72.56.107.196`, а Legacy healthcheck был green.

Frozen source inputs:

- Legacy SQLite: `481d5d6c9b54a58f78f288fb29c0eb072d43e74d6c2db8b14044a3153cd8f7f7`;
- all-issues evidence manifest:
  `9f6c488bbddd2975fa89a75d35348990814a85f79dcee0bd15a2fa513043f121`;
- deferred queue:
  `832d939f77857a5aaf9f81a19dc9ae962da19ba46b87a76b316a734d085604bf`;
- gazette asset:
  `1e6ba2bb055a2821bca2e05ad7ef6ec57e3a558049875ffc5e601c58911b637d`;
- accepted application package `app_release_20260819_545bf2e`:
  `85accde8b8c77c1fb8d10e84c267be77e7ca7af8e7fdc7e24e3dfcee02a727eb`.

## 3. Source full seed

Legacy importer был запущен с фиксированным `importedAt=2026-08-19T12:00:00Z`, после чего к
staging-копии была применена compatibility текущего application release. Новых schema migrations
не потребовалось: база уже содержала `0001` и `0002`.

Результат:

- content release: `rel_e404ff802c3e3c71083529ed`, sequence `0`;
- logical state:
  `ef5b4c3ef7ddfcda05c5aad331043bcc576ec641683e05d74ce1162e1e7c7f41`;
- full seed bytes: `4,898,816`;
- full seed SHA-256:
  `5970470c28db4998b07d21052e196e97e55c5cb0ddbd60e4671e0a5861ea54d9`;
- canonical manifest SHA-256:
  `9bb11abb69f2a195aeef00461ca4ee2c0cfb604d38d0c2075aa4d801e83232c5`;
- FTS projection SHA-256:
  `b1a473014506e310a621a9df09ccf8da5fd63b8b6eca436d59808695a97e7cda`.

Независимый export/import round-trip дал те же физические bytes, logical state, FTS hash и все
table counts/hashes.

## 4. Полный replicated contract

Source, exported seed, imported replica и Local Ru release совпали по inventory, row count и
logical SHA-256 каждой из 23 таблиц. Полные hashes сохранены в machine-readable evidence.

| Таблица | Строк |
| --- | ---: |
| `schema_migrations` | 2 |
| `application_compatibility` | 2 |
| `content_releases` | 1 |
| `source_snapshots` | 1 |
| `sources` | 45 |
| `materials` | 280 |
| `material_sources` | 280 |
| `material_evidence` | 244 |
| `editorial_queue` | 128 |
| `issues` | 74 |
| `legacy_issue_provenance` | 74 |
| `legacy_publication_evidence` | 518 |
| `issue_materials` | 254 |
| `issue_analysis` | 74 |
| `material_analysis` | 3 |
| `llm_attempts` | 483 |
| `source_rules` | 6 |
| `material_quality` | 254 |
| `rubrics` | 11 |
| `material_rubrics` | 728 |
| `daily_stats` | 74 |
| `gazettes` | 1 |
| `gazette_assets` | 1 |

Legacy `status='draft'` для всех 74 исторических выпусков не был ошибочно перенесён в public
lifecycle. Frozen publication evidence детерминированно классифицировал все 74 выпуска как
published, а исходный Legacy status сохранён в `legacy_issue_provenance`.

Private editorial state присутствует физически и сверена точно:

- `editorial_queue`: manual `0`, deferred `6`, review `122`;
- unassigned/private materials: `26`;
- snapshot `snp_f2746cdaf50ccc413336bb11`, item count `354`;
- snapshot manifest hash совпадает с frozen evidence manifest;
- snapshot payload hash совпадает с frozen Legacy SQLite.

## 5. Local Ru seed и activation

Seed был передан в private evidence/incoming и проверен до активации exact runtime:

- CPython `3.12.3`;
- SQLite `3.45.1`;
- exact SQLite source id;
- `58` compile options;
- application compatibility включает `app_release_20260819_545bf2e`.

Create-only immutable release:

`/var/lib/radar-v2/content/releases/2e72a051d06d52105f1b2d92b6c6727b.sqlite`

имеет owner `radar-v2-api:radar-v2-api`, mode `0600`, link count `1` и тот же физический SHA-256,
что source seed. Старый Stage 10 release и его pointer retained без удаления.

`active.json` был переключён атомарно. Сервис не перезапускался; API переоткрыл release по смене
pointer и вернул ожидаемые release/state markers.

## 6. Historical API acceptance

Loopback API проверен по всем 74 датам от `2026-06-07` до `2026-08-19` против Legacy production.
Совпали issue number/title/brief и стабильные material relations:

- выпусков: `74`;
- связей выпуска с материалами: `254`;
- пустых выпусков: `28`;
- LLM status: fallback `70`, success `4`;
- historical parity SHA-256:
  `955b5c24779c93ba7af9a054b572c047b29a024288f0fb49a247a963525d16da`.

Полная 11-endpoint matrix green: health/latest/issues/issue-by-date/materials/search/stats/
timeseries/rubrics/sources/gazettes. Дополнительно подтверждены `400` для malformed input, `404`
для internal path и `405` для write method.

Public invisibility gate:

- пересечение private/draft state с public issue rows: `0`;
- все 128 queue IDs отсутствуют в public responses;
- все 26 unassigned material IDs и их exact URLs отсутствуют в public responses;
- internal columns, host paths и private filesystem markers отсутствуют;
- итог: `leakage=none`.

До и после API matrix SHA-256 active database не изменился; SQLite sidecars не появились.

## 7. Historical correction

Correction выполнена только в retained disposable roots. Для пустого исторического выпуска
`2026-07-26` был построен штатный delta:

- `issues` upsert;
- финальный `content_releases` insert.

Delta apply создал новый release `rel_stage11_historical_correction_01`, sequence `1`, state
`7d322a1b0a364fee3d5235fc5c3e01ef003513f6a478d4094cccb0fddfb4ce36`; все 23 after-counts и
after-hashes совпали с независимо finalized target.

Publisher simulation доказала обе ветви:

1. успешная публикация открыла corrected title через public API;
2. принудительный post-activation smoke failure завершился `rolled_back`, восстановил исходные
   source/production release/state и исходный API title.

Живая Local Ru full-seed база correction не изменялась.

## 8. Content rollback/re-activation на Local Ru

Реальный content pointer был переключён:

```text
rel_e404ff802c3e3c71083529ed
  -> content_release_stage10_empty
  -> rel_e404ff802c3e3c71083529ed
```

На каждом шаге `/api/health` подтвердил exact release/state. Для empty release `/api/latest`
вернул ожидаемый `404`, после re-activation — `200`. PID оставался `23919`, `NRestarts=0`, обе
immutable SQLite сохранили исходные SHA-256.

### Найденный и устранённый дефект контрольного скрипта

Первый rehearsal создал новый inode pointer как `root:root 0600`. API работает как
`radar-v2-api`, поэтому не смог прочитать pointer и корректно ответил fail-closed HTTP `500`.
Публичного влияния не было: V2 остаётся loopback-only без Caddy vhost, а Legacy production не
изменялся.

Причина была доказана свежими one-shot manager checks: обе SQLite валидны, а ошибка находилась
только в security metadata pointer inode. Pointer был атомарно восстановлен с теми же canonical
bytes и owner `radar-v2-api:radar-v2-api`, health вернулся без service restart. Исправленный
rehearsal затем прошёл полностью.

Из этого следует обязательный Stage 13 invariant: remote pointer activation должна сохранять и
проверять bytes, UID, GID, mode, link count и directory durability; одного mode `0600` недостаточно.

## 9. Финальный non-regression

После всех проверок:

- active content SHA/state/release совпадают с source seed;
- pointer owner `radar-v2-api:radar-v2-api`, mode `0600`, link count `1`;
- `radar-v2-api.service` active+enabled, PID `23919`, `NRestarts=0`;
- listener только `127.0.0.1:8765`, внешний `147.45.99.225:8765` закрыт;
- application tree: exact manifest, `19` files, без drift/pycache;
- runtime tree: `747` members, exact Stage 10 manifest;
- Caddy config SHA не изменился и Radar vhost отсутствует;
- UFW byte-for-byte совпадает со Stage 10 baseline;
- семь NRD units active+enabled, `NRestarts=0`, `/api/health` green;
- error-priority journals Radar/NRD пусты за Stage 11;
- DNS через system resolver и Cloudflare остаётся `72.56.107.196`;
- Legacy database SHA не изменился, production healthcheck green.

## 10. Evidence

Source evidence:

- `/root/radar-stage11-source-evidence-20260820T042100Z`
- `/root/radar-stage11-correction-evidence-20260820T044000Z`

Local Ru evidence:

- `/root/radar-stage11-evidence-20260820T042100Z`

Ключевые machine-readable files:

- `source-acceptance.json`;
- `roundtrip-comparison.json`;
- `external-nonregression.json`;
- `correction-acceptance.json`;
- `acceptance/remote-preactivation.json`;
- `acceptance/activation.json`;
- `acceptance/historical-api-acceptance.json`;
- `acceptance/rollback-rehearsal.json`;
- `acceptance/final-nonregression.json`.

Все production releases, backup pointer и диагностические roots retained. Удалений не выполнялось.

## 11. Gate verdict и следующая граница

Stage 11 gate принят:

- source/Local Ru logical state совпадает;
- полный inventory/count/hash contract совпадает для всех 23 таблиц;
- 74 исторических выпуска доступны;
- private queues/provenance/snapshot физически присутствуют и публично невидимы;
- correction success/rollback green;
- живой content rollback/re-activation green;
- Legacy, NRD, Caddy, UFW и DNS non-regression green.

Следующий последовательный этап — Stage 12, отдельный shadow hostname и HTTPS. Он требует нового
явного разрешения, поскольку впервые изменит Caddy и внешний DNS, хотя основной Legacy hostname
останется без переключения.
