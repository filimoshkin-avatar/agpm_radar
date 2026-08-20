# Radar V2: архитектура, пересборка и миграция production на Local Ru

Дата исходного плана: 2026-08-19

Дата полной переработки: 2026-08-19

Статус: согласованный master plan; Stage 0, urgent Stage 0A и Stage 1–11 завершены

Целевой production-сервер: Local Ru, `147.45.99.225`

## 1. Назначение документа

Этот документ заменяет первоначальный план миграции из коммита `93b2630`.

Текущий AgPM Radar вырос как MVP: редакционный процесс, OpenClaw, LLM-вызовы, формирование DOCX, SQLite, публичный API, frontend и production-публикация связаны общими каталогами и не имеют формальных контрактов. Перенос существующего дерева на другой сервер не решает эту проблему.

Поэтому принято решение:

- не рефакторить работающий Legacy Radar на месте;
- построить Radar V2 как отдельную систему;
- сохранить Legacy Radar без изменений на всём этапе разработки и сравнения;
- публиковать V2 на Local Ru под отдельным shadow hostname;
- переключать основной домен только после явного решения владельца.

Документ фиксирует:

- границы Project Manager, Radar V2 и production;
- модель единой SQLite и её репликации;
- candidate, delta и release-контракты;
- daily, correction и gazette flows;
- безопасность публичного API;
- dual-run Legacy/V2;
- пошаговый план реализации, проверки, rollback и cutover.

## 2. Зафиксированные решения

Следующие решения считаются окончательными для первой версии Radar V2.

1. **Параллельная пересборка.** Legacy Radar не перестраивается и продолжает работать по старым правилам.
2. **Редакционный владелец — Project Manager.** Утренний cron, сбор, отбор, LLM-обработка, ручные очереди и взаимодействие с пользователем остаются в OpenClaw-сессиях Project Manager.
3. **Структурированный handoff.** Project Manager передаёт Radar V2 версионированный candidate package, а не правит production напрямую.
4. **Детерминированная публикация.** Проверки, запись в БД, генерация daily DOCX/JSON, построение delta, доставка и production smoke выполняются кодом Radar V2.
5. **Одна SQLite-модель.** На текущем сервере и Local Ru используются совместимые SQLite одной схемы со всеми draft и published строками.
6. **Полная логическая production-копия.** В production SQLite присутствуют все таблицы и строки, которые входят в контракт базы. Секреты, OAuth, raw HTML, полные LLM request/response-файлы и локальные filesystem-артефакты в неё не входят.
7. **Draft синхронизируется дискретно.** Draft-изменения попадают на Local Ru только внутри прошедшей проверки publisher-операции, а не непрерывно.
8. **Публичный API read-only.** API читает production SQLite только в `mode=ro`, не имеет write/admin endpoints и отдаёт только allowlisted published DTO.
9. **Исторический выпуск можно перезаписать.** Пользовательской модели revisions нет: исправление обновляет текущий выпуск. При этом publisher сохраняет закрытые backups, delta, before/after hashes и audit log для rollback.
10. **LLM не блокирует публикацию.** Выпуск публикуется при полном отказе LLM с детерминированным fallback-представлением и обязательным предупреждением в отчёте Project Manager.
11. **Техническая некорректность блокирует публикацию.** Нарушение схемы, целостности, безопасности, delta protocol или production smoke оставляет предыдущий release активным.
12. **Передача данных — row-level delta.** После первоначального full seed используются идемпотентные delta с base release, schema version, upserts, tombstones, hashes и транзакционным staging apply.
13. **Три release-потока.** Application, content и gazette releases независимы, но связаны compatibility contract.
14. **Синхронный publisher.** Project Manager вызывает одну CLI-команду и получает структурированный окончательный результат.
15. **Газета — immutable package.** HTML и assets публикуются версионированно; новый package может заменить текущую газету после проверок.
16. **Application deploy требует явного подтверждения.** Любой coding-агент может готовить изменения через git, но code release на Local Ru не выкатывается автоматически.
17. **Один immutable утренний input snapshot.** Legacy и V2 получают одинаковый исходный пул, но имеют отдельные правила, очереди, LLM-результаты, БД и публикационные состояния.
18. **Длительный dual-run.** Legacy и V2 работают параллельно до явного принятия V2 владельцем; фиксированного срока сравнения нет.
19. **Schema migrations отделены от content publishing.** Daily, correction и gazette candidates не содержат DDL/SQL и не меняют схему. Миграции выполняются только внутри явно подтверждённого application release под отдельным взаимоисключающим deploy lock.

## 3. Пользовательские сценарии

### 3.1. Ежедневный выпуск

Каждое утро Project Manager:

1. запускается своим OpenClaw cron;
2. собирает новости;
3. формирует immutable input snapshot;
4. обрабатывает snapshot по Legacy flow;
5. обрабатывает тот же snapshot по V2 flow;
6. применяет редакторские и LLM-правила V2;
7. формирует V2 candidate;
8. вызывает Radar V2 publisher;
9. получает подтверждение либо структурированный отказ;
10. отправляет пользователю отчёт о Legacy и V2.

Ожидаемый итог сообщения:

```text
Legacy Radar: выпуск опубликован, N материалов.
Radar V2: release <id> опубликован на Local Ru, N материалов.
LLM: primary failed, fallback succeeded.
Production checks: passed.
Различия Legacy/V2: ...
```

### 3.2. Исправление старого выпуска

Пользователь может написать Project Manager, например:

> В выпуске 2026-08-19 материал X дублирует материал Y из выпуска 2026-08-12. Проверь и убери дубль.

Project Manager:

1. анализирует оба материала и историю выпусков;
2. объясняет вывод пользователю;
3. после команды на изменение формирует correction candidate;
4. publisher проверяет ожидаемое текущее состояние выпуска;
5. обновляет исходную и production SQLite через delta;
6. повторно генерирует связанные JSON/DOCX/статистику;
7. выполняет production smoke;
8. сообщает пользователю результат.

Публично выпуск перезаписывается. Старое состояние сохраняется только как закрытый технический backup и audit evidence.

### 3.3. Публикация или изменение газеты

Пользователь может:

- приложить готовый HTML;
- попросить Project Manager изменить существующую газету;
- попросить собрать новый выпуск газеты.

Project Manager формирует gazette candidate package. Publisher проверяет package, публикует его на Local Ru, обновляет индекс газет и возвращает результат.

### 3.4. Разработка продукта

Claude Code, Codex, main OpenClaw, Project Manager или другой coding-агент могут:

- менять код в git;
- добавлять миграции;
- дорабатывать правила;
- менять API и frontend;
- добавлять тесты.

Но application release выполняется только из чистого проверенного commit/tag и после явного подтверждения пользователя.

## 4. Целевая архитектура

```text
Текущий сервер: control/dev/editorial plane

  Project Manager OpenClaw
    ├─ cron и диалог с пользователем
    ├─ сбор источников
    ├─ ручная очередь и deferred
    ├─ LLM-анализ и fallback
    ├─ immutable input snapshot
    ├─ Legacy branch
    └─ V2 candidate builder
             │
             ▼
  Radar V2 deterministic publisher
    ├─ candidate validation
    ├─ source DB staging copy
    ├─ content mutations без schema changes
    ├─ deterministic rules
    ├─ DOCX/JSON rendering
    ├─ delta construction
    ├─ local/remote state verification
    ├─ SSH delivery
    └─ structured final result
             │
             ▼
  Local Ru: public serving plane
    ├─ incoming quarantine
    ├─ production staging apply
    ├─ atomic DB release switch
    ├─ read-only API
    ├─ V2 frontend
    ├─ gazette static releases
    └─ Caddy HTTPS
```

## 5. Границы компонентов

### 5.1. Project Manager отвечает за

- расписание утреннего запуска;
- получение и агрегацию материалов;
- взаимодействие с внешними источниками;
- LLM-вызовы и fallback-модели;
- редакторский отбор;
- ручную очередь;
- разбор запросов пользователя;
- формирование candidate package;
- вызов publisher CLI;
- понятный итоговый отчёт пользователю.

Project Manager не должен:

- выполнять SQL на production;
- копировать live SQLite поверх активной базы;
- изменять Caddy/systemd;
- публиковать непроверенные файлы в public root;
- обходить publisher gates.

### 5.2. Radar V2 publisher отвечает за

- JSON Schema candidate contract;
- проверку optimistic concurrency/base state;
- миграции SQLite;
- применение candidate к staging DB;
- детерминированные инварианты;
- генерацию DOCX/JSON;
- delta package;
- SSH/rsync delivery;
- remote staging apply;
- атомарное переключение;
- smoke, audit и rollback;
- machine-readable результат.

Publisher не выполняет LLM-вызовы и не принимает редакторские решения.

### 5.3. Local Ru отвечает за

- хранение полной production SQLite;
- read-only публичный API;
- frontend V2;
- статические gazette releases;
- atomic activation и local healthchecks;
- сохранение ограниченного набора rollback releases.

На Local Ru отсутствуют:

- OpenClaw;
- LLM credentials;
- сборщики источников;
- daily cron;
- Telegram;
- публичные write/admin endpoints.

## 6. Репозиторий Radar V2

Целевая структура:

```text
agpm-radar/
  apps/
    api/
    web/
  packages/
    contracts/
    domain/
    storage/
    publisher/
    delta/
    renderers/
      daily-json/
      daily-docx/
      gazette/
    validation/
    legacy-bridge/
  adapters/
    openclaw-project-manager/
    ssh-production/
  database/
    migrations/
    seeds/
    schema/
  deploy/
    source/
    production/
    scripts/
  tests/
    unit/
    integration/
    golden/
    security/
    migration/
    dual-run/
  fixtures/
    minimal/
    legacy-import/
    no-llm/
    correction/
    gazette/
  docs/
    architecture.md
    data-model.md
    candidate-contract.md
    delta-contract.md
    operations-project-manager.md
    operations-publisher.md
    production-runbook.md
    rollback-runbook.md
    dual-run.md
```

Технологический стек должен быть выбран в первом implementation ADR. Требования независимо от языка:

- воспроизводимая lockfile-сборка;
- поддержка SQLite и FTS5;
- строгие схемы входных данных;
- тестируемая CLI;
- production build без dev-зависимостей;
- отсутствие OpenClaw SDK/runtime в public API artifact.

## 7. Модель единой SQLite

### 7.1. Общие правила

- Схема source и production идентична.
- Схема изменяется только явно подтверждённым application release, не daily/correction/gazette publication.
- Внешние evidence-файлы не кодируются как локальные абсолютные пути в публичных DTO.
- Все timestamps хранятся в UTC ISO-8601.
- Все идентификаторы стабильны и не зависят от title.
- Foreign keys включены для каждого writer-соединения.
- Production API использует URI `file:...?...mode=ro` и `PRAGMA query_only=ON`.
- Production writer только publisher/deploy service.
- `stateHash` означает deterministic logical hash versioned schema и отсортированных строк всех replicated tables; это не SHA-256 физического SQLite-файла, page layout которого может различаться.

### 7.2. Минимальные доменные сущности

Предлагаемый логический набор таблиц:

- `schema_migrations` — применённые миграции;
- `source_snapshots` — immutable утренние snapshots;
- `materials` — нормализованные материалы;
- `material_sources` — источники обнаружения материала;
- `material_evidence` — безопасные metadata/evidence references без локальных секретных путей;
- `editorial_queue` — manual/deferred/review state;
- `issues` — выпуски и их `draft|published` состояние;
- `issue_materials` — связь выпуска и материалов, порядок, редакторские поля;
- `issue_analysis` — LLM или deterministic fallback для выпуска;
- `material_analysis` — LLM/fallback карточек;
- `rubrics` и `material_rubrics`;
- `daily_stats`;
- `gazettes` — текущая запись газеты;
- `gazette_assets` — manifest статических assets;
- `content_releases` — финальные реплицируемые release/base/schema/hash markers;
- `application_compatibility` — допустимые schema/API versions.

Текущее состояние publisher job, transport attempts и host-local audit не хранятся в реплицируемой SQLite: иначе source и production перестанут быть идентичными. Они ведутся как защищённые append-only JSON/JSONL journals и release packages на каждом соответствующем host. Финальный `content_releases` marker включается в staging DB до вычисления итогового hash и одинаково активируется на обоих серверах.

Физическая схема уточняется ADR и миграциями. Публичный API не должен использовать `SELECT *` из внутренних таблиц.

### 7.3. Draft и published

Вариант реализации выбирается ADR:

- отдельные draft/published таблицы; либо
- общие таблицы с явным lifecycle/status и проверяемыми views.

Обязательные свойства:

- каждый public query содержит published boundary;
- draft не может попасть в `/api/latest`, `/api/issues`, search, stats или FTS;
- пустой опубликованный выпуск допустим;
- LLM status не определяет published status;
- удаление материала из выпуска удаляет связь `issue_materials`, а не обязательно глобальную запись материала;
- исправление выпуска обновляет текущую опубликованную модель на месте.

### 7.4. Что реплицируется

Реплицируются все таблицы/строки контрактной SQLite, включая:

- drafts;
- published issues;
- editorial queue;
- LLM statuses и безопасные результаты;
- финальные content release markers, необходимые для согласования базы;
- gazette metadata.

Не реплицируются через SQLite:

- API keys и OAuth;
- OpenClaw profiles/sessions;
- raw HTML;
- полные сырые provider responses;
- локальные абсолютные пути;
- Telegram metadata, не являющиеся доменными данными;
- большие DOCX/HTML/assets: они идут отдельными release files с checksums.

## 8. Immutable input snapshot и dual-run

### 8.1. Snapshot

После утреннего сбора Project Manager создаёт immutable snapshot:

```text
snapshots/YYYY-MM-DD/<snapshot-id>/
  manifest.json
  candidates.jsonl
  safe-evidence-index.json
  checksums.sha256
```

Snapshot фиксирует один и тот же вход для Legacy и V2. После создания он не изменяется.

Обе ветви обязаны записать consumption attestation с:

- `snapshotId`;
- SHA-256 точных bytes `manifest.json`;
- SHA-256 `checksums.sha256`;
- aggregate hash всех входных payload-файлов.

Одинаковый `snapshotId` без совпадения этих hashes не считается одинаковым входом.

### 8.2. Legacy branch

Legacy branch:

- использует старые правила;
- использует отдельный legacy corpus, deferred и SQLite;
- публикует на текущем сервере;
- продолжает обслуживать `radar.aipractice.space` до cutover;
- не зависит от готовности V2.

### 8.3. V2 branch

V2 branch:

- читает тот же input snapshot;
- имеет отдельную SQLite и queues;
- применяет новые правила;
- публикует на Local Ru shadow hostname;
- не пишет в Legacy каталоги.

### 8.4. Сравнение

Для каждого дня генерируется comparison report:

- snapshot id;
- snapshot manifest/payload hashes обеих ветвей;
- количество входных материалов;
- Legacy included/rejected/deferred;
- V2 included/rejected/deferred;
- только Legacy;
- только V2;
- различия рубрик, дат, дублей и статистики;
- LLM provider/fallback statuses;
- release/health status обеих ветвей.

## 9. Candidate contract

### 9.1. Общий package

```text
candidate/<candidate-id>/
  manifest.json
  payload/
    issue.json
    materials.json
    analyses.json
    stats.json
    replication-mutations.json
    snapshot-attestation.json
  assets/
  checksums.sha256
```

### 9.2. Обязательные поля manifest

```json
{
  "contractVersion": "1",
  "candidateId": "...",
  "operation": "daily|correction|gazette",
  "issueDate": "2026-08-19",
  "snapshotId": "...",
  "snapshotManifestSha256": "...",
  "snapshotPayloadSha256": "...",
  "expectedBaseReleaseId": "...",
  "expectedIssueStateHash": "...",
  "schemaVersion": 1,
  "createdAt": "...Z",
  "initiator": "project-manager",
  "reason": "daily publish",
  "llm": {
    "status": "success|fallback|unavailable",
    "attempts": []
  },
  "files": [],
  "checksums": {}
}
```

### 9.3. Candidate invariants

- `candidateId` и idempotency key уникальны;
- `schemaVersion` точно совпадает с активной application compatibility; content candidate не может содержать migration/DDL/SQL;
- correction указывает ожидаемый hash изменяемого выпуска;
- `replication-mutations.json` содержит типизированные upsert/unlink/tombstone-операции для всех затронутых replicated entities, включая snapshot, drafts и editorial queue;
- manifest явно перечисляет затронутые replicated tables и ожидаемое число мутаций по каждой таблице;
- daily candidate подтверждает точные snapshot manifest/payload hashes, а не только `snapshotId`;
- все material ids стабильны;
- порядок материалов явный;
- duplicate override имеет причину;
- пустой выпуск содержит явный `emptyReason`;
- LLM unavailable допустим;
- неизвестные поля отклоняются либо обрабатываются по явной compatibility policy;
- package не содержит секретов или абсолютных путей.

## 10. Publisher state machine

```text
RECEIVED
  -> VALIDATED
  -> SOURCE_STAGED
  -> ARTIFACTS_BUILT
  -> DELTA_BUILT
  -> REMOTE_STAGED
  -> REMOTE_VERIFIED
  -> REMOTE_ACTIVE
  -> API_RELOADED
  -> LOOPBACK_VERIFIED
  -> PUBLIC_VERIFIED
  -> SOURCE_COMMITTED
  -> SUCCEEDED
```

Ошибочные состояния:

```text
REJECTED
FAILED_PRE_ACTIVATION
FAILED_POST_REMOTE_ACTIVATION
REMOTE_ROLLBACK_ACTIVE
ROLLBACK_API_RELOADED
ROLLBACK_VERIFIED
ROLLED_BACK
NEEDS_RECONCILIATION
```

State machine и retry checkpoints записываются во внешний защищённый publisher journal, а не в реплицируемую SQLite.

`REMOTE_ACTIVE` является только provisional activation. Release считается успешным исключительно после ожидаемого loopback/public release id/hash, source commit и перехода в `SUCCEEDED`.

Повторный запуск с тем же candidate id:

- не создаёт вторую публикацию;
- возвращает сохранённый `SUCCEEDED`; либо
- продолжает безопасно с последнего recoverable шага.

## 11. Daily/correction publication algorithm

1. Получить exclusive content publisher lock, взаимоисключающий с application/migration deploy lock.
2. Проверить доступность внешнего audit journal и заранее записать pending operation.
3. Проверить candidate schema, checksums и отсутствие SQL/DDL/migration payload.
4. Проверить точное совпадение `schemaVersion` с активной application compatibility.
5. Проверить `expectedBaseReleaseId`.
6. Проверить optimistic concurrency для correction.
7. Проверить snapshot id и точные manifest/payload hashes для daily candidate.
8. Создать filesystem-consistent staging copy source SQLite.
9. Применить типизированные candidate mutations к source staging DB в транзакции, включая затронутые snapshot/draft/queue rows.
10. Выполнить DB checks:
   - `PRAGMA integrity_check`;
   - `PRAGMA foreign_key_check`;
   - migration count/version;
   - draft/published invariants;
   - issue/material/stats consistency;
   - отсутствие draft leakage в public views;
   - отсутствие запрещённых путей/полей;
   - per-table row counts/hashes для **всех** replicated tables.
11. Сгенерировать daily JSON и DOCX детерминированными renderers.
12. Проверить JSON Schema, DOCX structure и checksums.
13. Вычислить delta между текущей canonical DB и source staging DB по полному allowlist replicated tables.
14. Проверить, что declared candidate mutation coverage и фактический DB diff совпадают; необъявленный drift блокирует release.
15. Сформировать delta manifest и expected final state hash; schema version до/после обязана быть одинаковой.
16. Передать package на Local Ru в unique incoming directory.
17. Remote activator проверяет ownership, mode, manifest и checksums.
18. Сохранить previous remote content pointer, release id и state hash.
19. Создать staging copy текущей production DB.
20. Проверить production base release/hash.
21. Применить delta в одной транзакции.
22. Выполнить integrity/FK/schema и per-table row-count/hash checks для полного replicated contract.
23. Проверить, что remote final state hash равен source staging hash.
24. Установить DB и generated daily artifacts в единый immutable content release directory.
25. Атомарно переключить один remote content current pointer.
26. Принудительно заставить API переоткрыть SQLite: controlled reload/restart либо доказанный reload hook; дождаться готовности.
27. Проверить loopback API и потребовать ожидаемые `releaseId`, `schemaVersion` и `stateHash`.
28. Проверить public shadow HTTPS frontend/API и те же ожидаемые release markers.
29. Атомарно зафиксировать source canonical staging DB с тем же final release marker/hash.
30. Проверить source final state hash.
31. Записать `SUCCEEDED` во внешний append-only audit journal без изменения уже сверенных DB.
32. Освободить lock.
33. Вернуть Project Manager структурированный итог.

При **любой** ошибке после remote pointer switch и до успешного source commit publisher обязан:

1. перейти в `FAILED_POST_REMOTE_ACTIVATION`;
2. вернуть remote pointer на сохранённый previous release;
3. снова принудительно переоткрыть SQLite в API;
4. проверить previous `releaseId` и `stateHash` через loopback и public HTTPS;
5. оставить source canonical DB неизменённой;
6. записать `ROLLED_BACK` и не сообщать об успешной публикации.

Если rollback либо его проверка не удались, publisher ставит `NEEDS_RECONCILIATION`, блокирует следующие content releases и немедленно сообщает Project Manager критическую ошибку. Неуспешный release остаётся в quarantine для расследования.

## 12. Delta protocol

### 12.1. Initial seed

Первый перенос выполняется полным SQLite snapshot:

- источник quiescent либо online backup API;
- checksum;
- schema manifest;
- row counts;
- table hashes;
- integrity/FK checks до и после передачи.

### 12.2. Delta package

```text
delta/<release-id>/
  manifest.json
  upserts.jsonl
  tombstones.jsonl
  assets/
  checksums.sha256
```

Manifest содержит:

- release id;
- base release id;
- candidate id;
- schema version до/после; для content release значения обязаны совпадать;
- таблицы и количество операций;
- before/after DB state hash;
- ожидаемые row counts;
- hashes критичных таблиц;
- application compatibility range;
- asset checksums.

### 12.3. Обязательные свойства

- строгий allowlist таблиц и колонок;
- параметризованный SQL, без SQL из package;
- deterministic operation order;
- upsert keys определены схемой;
- tombstones явны;
- повторное применение безопасно;
- base mismatch блокирует apply;
- gap в release sequence блокирует apply;
- всё применяется к staging copy, не live DB;
- активная DB никогда не модифицируется на месте.
- delta охватывает каждую изменившуюся строку полного allowlist replicated tables, включая drafts, `editorial_queue` и `source_snapshots`;
- completeness проверяется per-table row counts/hashes, а не только публичными endpoint-значениями.

### 12.4. Reconciliation

Ежедневно после публикации:

- сравниваются release id, schema version, row counts и critical table hashes.

Периодически:

- выполняется полный DB logical hash/reconciliation;
- при drift delta-публикации приостанавливаются;
- формируется новый full seed;
- причина drift расследуется до возобновления.

## 13. LLM policy

LLM status:

- `success` — основная модель отработала;
- `fallback` — отработала резервная модель или deterministic fallback;
- `unavailable` — ни одна LLM не дала результата.

`unavailable` не блокирует публикацию, если:

- candidate структурно корректен;
- обязательные non-LLM поля заполнены;
- frontend имеет fallback rendering;
- Project Manager включает предупреждение в финальный отчёт.

Технические проверки не ослабляются из-за LLM failure.

## 14. Public API contract

### 14.1. Требования

- bind только на loopback;
- Caddy — единственная публичная точка входа;
- dedicated non-root user;
- SQLite `mode=ro` + `query_only`;
- нет write methods/endpoints;
- нет `/api/internal/*`;
- нет `SELECT *` в public DTO layer;
- только published rows;
- health не раскрывает filesystem paths;
- bounded query parameters;
- invalid input возвращает 4xx JSON, а не рвёт соединение;
- request timeout и response size limits;
- rate limiting на тяжёлые search endpoints;
- same-origin CORS либо отсутствие CORS, если cross-origin не нужен;
- структурированные logs без содержимого материалов и секретов;
- API открывает новую read-only connection на запрос либо реализует доказанный connection reload; activator в любом случае выполняет controlled reload/restart после смены content pointer;
- `/api/health` возвращает безопасные `releaseId`, `schemaVersion` и `stateHash`, но не filesystem path;
- loopback/public smoke сравнивает эти markers с ожидаемым release, поэтому ответ со старого SQLite inode не может считаться успешным.

### 14.2. Минимальные endpoints

Точный API определяется OpenAPI contract. Минимум:

- `/api/health`;
- `/api/issues`;
- `/api/issues/{date}`;
- `/api/latest`;
- `/api/materials`;
- `/api/search`;
- `/api/stats`;
- `/api/timeseries`;
- `/api/rubrics`;
- `/api/sources`;
- `/api/gazettes`.

Каждый endpoint имеет explicit DTO и tests на draft leakage.

## 15. Frontend V2

Frontend не копируется механически. Требования:

- API base URL same-origin `/api`;
- cache-busted immutable assets;
- HTML `no-store` либо корректная revalidation policy;
- корректная работа latest, historical issue, search, filters и empty issue;
- явный fallback при `llm.status=unavailable`;
- отсутствие отображения внутренних IDs/paths;
- доступность keyboard/mobile;
- CSP-compatible implementation;
- отдельная gazette view без hardcoded имени одного HTML-файла;
- frontend показывает только API contract, не знает SQLite schema.

## 16. Gazette release flow

### 16.1. Package

```text
gazette/<gazette-release-id>/
  manifest.json
  index.html
  assets/
  checksums.sha256
```

### 16.2. Проверки

- manifest/schema;
- unique gazette id и period;
- HTML parse;
- отсутствие опасных внешних scripts/forms/write calls;
- allowlist внешних ссылок или явный warning policy;
- bundled/local assets либо явно разрешённые dependencies;
- отсутствие draft/service blocks;
- link check;
- desktop/mobile visual smoke;
- print smoke;
- CSP compatibility;
- checksum verification.

### 16.3. Публикация

- package переносится в immutable directory;
- gazette metadata обновляется в SQLite через тот же publisher;
- current/index переключается атомарно;
- старое состояние перезаписывается публично, но предыдущий artifact сохраняется как технический rollback release;
- public API и frontend index проверяются после активации.

## 17. Release topology

### 17.1. Application release

Содержит:

- API artifact;
- frontend artifact;
- migrations;
- OpenAPI/schema versions;
- compatibility manifest;
- systemd/Caddy templates;
- checksums и commit/tag provenance.

Требует явного подтверждения пользователя.

Если application release содержит schema migrations, он:

- получает отдельный application/migration lock, взаимоисключающий с content publisher lock;
- приостанавливает новые content releases;
- применяет одну и ту же versioned migration к source и production **staging copies**, не к active DB;
- проверяет integrity/FK, полный replicated table contract и application compatibility;
- активирует совместимые application/DB releases по документированной последовательности;
- имеет обязательный coordinated rollback приложения и обеих DB;
- возобновляет content publishing только после подтверждённого одинакового schema version.

Daily, correction и gazette content packages не могут переносить или запускать migrations.

### 17.2. Content release

Содержит:

- DB delta;
- daily JSON/DOCX;
- release manifest;
- hashes и audit metadata.

Публикуется автоматически после успешных checks.

### 17.3. Gazette release

Содержит:

- HTML/assets;
- gazette metadata delta;
- release manifest.

Публикуется после явного пользовательского запроса через Project Manager.

## 18. Production layout на Local Ru

Предлагаемые пути:

```text
/opt/radar-v2-api/
  releases/<application-release-id>/
  current -> releases/...

/srv/radar-v2.aipractice.space/
  releases/<application-release-id>/
  current -> releases/...

/var/lib/radar-v2/
  content/releases/<content-release-id>/
    radar.sqlite
    artifacts/
  content/current -> releases/...
  gazettes/releases/<gazette-release-id>/
  incoming/<release-id>/
  audit/
  backups/

/etc/radar-v2/
  api.env
  deploy.env
```

Имена hostname/path уточняются перед stage deployment. До этого используется placeholder `radar-v2.aipractice.space`.

## 19. Production security

### 19.1. Users

- `radar-v2-api` — read-only API;
- `radar-v2-deploy` — controlled activation;
- Caddy читает только frontend/gazette current trees;
- root не используется для runtime API.

### 19.2. SSH deploy

- отдельный ключ;
- отдельный user;
- restricted command/allowlisted deploy entrypoint;
- no interactive shell, если практично;
- incoming quarantine;
- ownership/mode verification;
- package никогда не распаковывается напрямую в active path.

### 19.3. systemd

Минимум:

- `NoNewPrivileges=true`;
- `PrivateTmp=true`;
- `ProtectSystem=strict`;
- `ProtectHome=true`;
- `ProtectKernelTunables=true`;
- `ProtectControlGroups=true`;
- `RestrictSUIDSGID=true`;
- `RestrictNamespaces=true`;
- `LockPersonality=true`;
- `MemoryDenyWriteExecute=true`, если runtime совместим;
- явные `ReadOnlyPaths`/`ReadWritePaths`;
- memory/tasks limits;
- restart policy и startup health.

### 19.4. Caddy

- отдельный shadow vhost;
- HTTPS;
- API и SPA в разных `handle` blocks;
- security headers;
- cache policy;
- no directory listing;
- access logs;
- internal ports loopback-only.

## 20. Testing strategy

### 20.1. Unit

- domain rules;
- status transitions;
- duplicate detection;
- fallback rendering;
- candidate validation;
- delta generation/apply;
- tombstones;
- hash calculation;
- DTO allowlists.

### 20.2. Integration

- empty DB bootstrap;
- Legacy import;
- daily candidate success;
- no-LLM daily success;
- correction overwrite;
- material unlink/delete semantics;
- gazette create/update;
- source/production state equality;
- idempotent retry;
- base mismatch;
- content candidate со schema mismatch/DDL/SQL отклоняется;
- полный draft/queue/snapshot mutation path и per-table equivalence;
- interrupted transfer;
- interrupted activation;
- API reload/reopen после pointer switch;
- loopback/public release marker mismatch;
- обязательный rollback после каждого post-activation failure;
- rollback verification failure переводит систему в `NEEDS_RECONCILIATION`.

### 20.3. Golden

Golden fixtures должны покрыть:

- обычный выпуск;
- пустой выпуск;
- duplicate correction;
- неверную дату публикации;
- manual material;
- все LLM unavailable;
- historical issue overwrite;
- gazette HTML package.

### 20.4. Security

- draft leakage по всем endpoints;
- absolute path leakage;
- unsafe HTML;
- SQL injection;
- FTS malformed query;
- invalid numeric limits;
- oversized query/response;
- forbidden methods;
- symlink/path traversal;
- malicious package filenames;
- manifest checksum mismatch.

### 20.5. Visual

- desktop/mobile;
- latest/historical;
- no-LLM fallback;
- empty issue;
- search/filter;
- gazette/print;
- no console errors;
- no horizontal overflow;
- accessibility smoke.

## 21. Observability и отчёт Project Manager

Publisher возвращает JSON:

```json
{
  "status": "published",
  "candidateId": "...",
  "releaseId": "...",
  "operation": "daily",
  "issueDate": "2026-08-19",
  "sourceStateHash": "...",
  "productionStateHash": "...",
  "llmStatus": "unavailable",
  "checks": {
    "database": "passed",
    "api": "passed",
    "frontend": "passed"
  },
  "warnings": ["All LLM providers unavailable; deterministic fallback published"]
}
```

Project Manager обязан преобразовать результат в понятное сообщение, но не менять его смысл.

Отсутствие финального сообщения Project Manager считается ошибкой daily workflow.

## 22. Backup и disaster recovery

- backup source SQLite перед каждым publisher commit;
- backup production SQLite/current pointers перед activation;
- delta packages и manifests сохраняются по retention policy;
- application/gazette immutable releases сохраняются минимум до окончания shadow/observation;
- регулярный off-host backup source и production state;
- documented restore from full seed + ordered deltas;
- documented restore from latest verified full backup;
- ежемесячный restore drill до cutover и после значимых schema changes.

Хотя продуктовая модель использует overwrite, технические backups обязательны и не являются пользовательскими revisions.

## 23. Пошаговый план реализации

Ниже этапы выполняются последовательно. Переход к следующему этапу разрешён только после acceptance gate предыдущего.

## Этап 0. Заморозить и измерить Legacy baseline

### Работы

1. Зафиксировать commit/hash текущего Legacy кода.
2. Инвентаризировать фактический Project Manager cron и его owner/session/state.
3. Снять read-only inventory:
   - SQLite schema/version/table counts;
   - corpus/queues;
   - daily inputs/outputs;
   - API surface;
   - Caddy/systemd;
   - gazette artifacts;
   - backups/logs.
4. Зафиксировать текущие правила и known fallbacks как Legacy contract.
5. Подготовить baseline fixtures из нескольких выпусков без секретов.
6. Не менять Legacy pipeline.

### Gate

- baseline report сохранён;
- Legacy production health green;
- cron ownership доказан;
- fixtures воспроизводимы;
- нет незакоммиченных изменений, которые не классифицированы.

### Rollback

Не требуется: этап read-only.

## Этап 0A. Срочно закрыть Legacy backup artifacts

Статус: completed 2026-08-19. Evidence: `docs/radar-stage0a-caddy-containment-2026-08-19.md`.

Этот межэтап добавлен по результатам Stage 0: static root сервится прямо из Legacy worktree, и 43 ignored backup-файла фактически доступны по public URL.

### Работы

1. Сохранить timestamped backup текущего Caddyfile и его SHA-256.
2. Добавить fail-closed matcher для backup/temp/source-map patterns до SPA fallback.
3. Провалидировать Caddy config и выполнить graceful reload.
4. Проверить representative backup URLs, encoded path variants и unrelated public hosts.
5. Не удалять Legacy backup-файлы и не менять Radar pipeline/API/frontend.

### Gate

- backup/temp/source-map URLs возвращают fail-closed 404;
- public frontend, API и активные assets отвечают штатно;
- Caddy active, config valid, restarts/errors не появились;
- ни один Legacy-файл не удалён.

### Rollback

Вернуть timestamped Caddyfile backup, повторить validation и graceful reload.

### Результат

- все 43 сохранённых backup artifacts и encoded variants возвращают HTTP 404;
- active UI/API/assets/gazette и unrelated hosts сохранили baseline HTTP 200;
- Caddy reload был graceful: PID прежний, `NRestarts=0`, warning/error journal пуст;
- Legacy DB, API source, frontend и pipeline не изменились;
- файлы не удалялись; rollback backup и evidence сохранены.

## Этап 1. Архитектурные контракты и ADR

Статус: completed 2026-08-19. Evidence: `docs/radar-stage1-contracts-2026-08-19.md`.

### Работы

1. Создать architecture/data model ADR.
2. Зафиксировать SQLite runtime/version/FTS5 contract.
3. Утвердить physical draft/published schema.
4. Утвердить candidate JSON Schema.
5. Утвердить delta schema и table/column allowlist.
6. Утвердить public OpenAPI DTO.
7. Утвердить publisher state machine и exit codes.
8. Утвердить application/content/gazette compatibility contract.
9. Утвердить error taxonomy и Project Manager report contract.
10. Утвердить historical-publication inference: Legacy `status='draft'` не является V2 draft; публичность доказывается согласованным набором Legacy report/API/corpus evidence, а исходные status/published_at сохраняются как provenance.
11. Утвердить LLM outcome contract: requested model, attempted models, effective model и `success|fallback|unavailable`; fallback/no-LLM не блокируют structurally valid content release.

### Gate

- все contracts versioned;
- примеры проходят schema validators;
- нет абсолютных production paths в domain contracts;
- нет неоднозначных writer roles.

### Результат

- приняты ADR-0001/0002;
- создан contract family `contracts/v1` с closed candidate branches, generated typed delta, exhaustive result/report conditions, compatibility variants, SQLite/state/error/history/OpenAPI contracts;
- заморожен per-artifact evidence manifest всех 74 Legacy-выпусков;
- positive/cross-contract/negative validation проходит;
- runtime и production не менялись.

## Этап 2. Создать изолированный Radar V2 repository skeleton

Статус: completed 2026-08-19. Evidence: `docs/radar-stage2-skeleton-2026-08-19.md`.

### Работы

1. Создать V2 packages/apps/tests structure.
2. Настроить formatter, lint, typecheck/build/test.
3. Добавить dependency lockfile.
4. Добавить secret scan и artifact manifest check.
5. Добавить CI/local verification entrypoint.
6. Добавить fixtures без production данных.
7. Не подключать V2 к cron или production.

### Gate

- clean install/build/test проходит;
- production artifact excludes tests/dev/OpenClaw credentials;
- repository не содержит секретов/DB/raw corpus.

### Результат

- создан изолированный `v2/` с Python 3.12 API и обязательными package boundaries;
- зафиксированы `pyproject.toml`, `uv.lock`, Ruff, strict mypy, pytest и exact SQLite build profile;
- dependency-free web ES module проверяется Node без npm/runtime dependencies;
- единый local/CI entrypoint запускает parent Stage 1 validator, secret/isolation scan и все quality gates;
- allowlist-only production artifact строится детерминированно и проходит membership/manifest audit;
- используются только явно маркированные synthetic fixtures; Legacy runtime/config/data/cron и Local Ru не менялись.

## Этап 3. Реализовать V2 SQLite и Legacy importer

Статус: completed and accepted 2026-08-19. Evidence:
`docs/radar-stage3-sqlite-importer-2026-08-19.md`.

### Работы

1. Реализовать migration runner.
2. Создать initial V2 schema.
3. Реализовать deterministic IDs.
4. Реализовать draft/published boundary.
5. Реализовать replicated content release markers и внешний append-only publisher audit journal.
6. Реализовать importer всех доступных Legacy domain states: выпуски, материалы, rubrics, stats, analyses, manual/deferred/review queues, gazette metadata и безопасные snapshot metadata.
7. Для каждой контрактной таблицы определить import source, derivation rule либо явное доказательство допустимого пустого initial state.
8. Нормализовать связи issue/material вместо неявного удаления материалов.
9. Удалить локальные пути из публичных полей.
10. Добавить source/production DB equivalence tool по полному списку replicated tables.
11. Реализовать historical-publication inference для Legacy: все доказанно публичные выпуски получают V2 published lifecycle, даже если Legacy хранит `status='draft'` и `published_at=NULL`; оригинальные значения сохраняются в provenance.
12. Не смешивать inferred historical publications с реальными V2 editorial drafts и queues.

### Gate

- весь исторический корпус импортируется в disposable DB;
- integrity/FK checks green;
- для каждой контрактной таблицы зафиксированы source, row count и table hash; необъяснимо пустая draft/queue/snapshot таблица блокирует gate;
- counts и публичные значения сопоставлены с Legacy;
- drafts не попадают в public views;
- 74 исторически публичных Legacy-выпуска распознаны по inference contract, а не по ошибочному Legacy status;
- повторный import идемпотентен либо явно запрещён после bootstrap.

### Результат

- реализованы 23 replicated tables, 8 published-only views, FTS5, deterministic migrations/IDs,
  logical hashing, bootstrap seal и source/replica equivalence;
- frozen Legacy corpus импортирован в две disposable DB: 74 published issues, 254 issue materials,
  280 materials, 128 queue rows, 483 normalized outcome rows и 1 gazette/asset;
- обе DB mode `0600`, побайтно идентичны; logical state SHA-256
  `ef5b4c3ef7ddfcda05c5aad331043bcc576ec641683e05d74ce1162e1e7c7f41`, file SHA-256
  `e285e439df3ebaef777b35e7e26b1a49c89a99f5ce8a0db7988310a6af906f1c`;
- integrity/FK, per-table count/hash, complete FTS projection and FTS integrity checks green;
- external audit journal uses a locked, append-only, hash-chained and fsynced write path; thread and
  process concurrency regressions green;
- 472 deterministic Legacy fallbacks no longer masquerade as LLM success; 11 accepted model calls
  remain `success`;
- Legacy production DB remained byte-identical; runtime, cron, Caddy, Local Ru and DNS unchanged.

## Этап 4. Реализовать immutable input snapshot и Legacy/V2 fork

### Работы

1. Выделить boundary после общего сбора.
2. Создать snapshot manifest/checksums.
3. Подключить Legacy branch к копии snapshot без изменения старых правил.
4. Подключить V2 branch к отдельной копии snapshot.
5. Записывать consumption attestation обеих ветвей с SHA-256 manifest, checksum file и aggregate payload.
6. Разделить queues/corpus/DB/logs.
7. Реализовать daily comparison report.
8. Пока не публиковать V2 наружу.

### Gate

- обе ветви используют не только один snapshot id, но и точные canonical manifest/checksum/payload hashes;
- изменение любого snapshot byte после создания обнаруживается и блокирует V2 candidate;
- Legacy output совпадает с baseline в допустимых пределах;
- сбой V2 не блокирует Legacy;
- V2 не пишет в Legacy paths.

### Результат

Stage 4 завершён и зафиксирован в
`docs/radar-stage4-snapshot-fork-2026-08-19.md`. Реализованы canonical four-file snapshot,
обязательная creation identity, отдельные verified Legacy/V2 copies, private consumption
attestations, pinned-directory reads, capability-scoped branch workspaces, fail-closed exact Legacy
baseline gate, runtime result validation, независимая обработка ошибок и canonical daily comparison.
V2 publication и Stage 5 candidate builder не реализованы.

## Этап 5. Candidate builder и Project Manager adapter

### Работы

1. Реализовать candidate builder CLI/library.
2. Реализовать daily candidate.
3. Реализовать correction candidate с expected issue hash.
4. Реализовать gazette candidate.
5. Реализовать типизированный `replication-mutations.json` для snapshots, drafts, queues и остальных затронутых replicated tables без произвольного SQL.
6. Реализовать completeness declaration/counts по затронутым таблицам.
7. Реализовать secret/path scrubbing.
8. Реализовать Project Manager playbooks:
   - daily;
   - correction;
   - gazette;
   - retry/status.
9. Добавить human-readable preview и machine-readable package.
10. Включить в candidate/report фактический LLM outcome: requested/attempted/effective model, fallback status и предупреждение при полном отказе LLM.

### Gate

- все fixture candidates проходят schema;
- malformed packages rejected;
- повтор candidate id детектируется;
- schema mismatch, DDL и SQL payload отклоняются;
- draft/queue/snapshot fixture mutations входят в candidate и воспроизводятся в staging DB;
- Project Manager может сформировать candidate без прямого SQL.
- успешный fallback и no-LLM candidate дают однозначный machine-readable result и не маскируются как primary-model success.

### Результат

Stage 5 завершён и зафиксирован в `docs/radar-stage5-candidate-builder-2026-08-19.md`.
Реализованы dependency-free daily/correction/gazette builders, строгая runtime-валидация frozen
contract v1, Project Manager CLI/playbooks, типизированные full-row mutations с optimistic
preconditions и completeness counts, перенос текущих drafts/editorial queue/snapshot evidence,
replay в новую staging SQLite, immutable nested package с preview/checksums и final-report adapter.
Candidate не может авторить `content_releases`, SQL/DDL, migrations, schema metadata или FTS.
Publication и production integration не реализованы.

## Этап 6. Детерминированные renderers и validators

### Работы

1. Реализовать public JSON renderer.
2. Реализовать daily DOCX renderer.
3. Реализовать no-LLM fallback.
4. Реализовать stats/duplicate/date invariants.
5. Реализовать gazette validator.
6. Добавить golden snapshots.
7. Сравнить DOCX/JSON V2 с Legacy на historical fixtures.

### Gate

- одинаковый input даёт byte-stable либо semantically stable output по контракту;
- no-LLM fixture публикуем;
- invalid DB/candidate блокируется;
- DOCX открывается и проходит structure checks.

### Результат

Stage 6 завершён и зафиксирован в `docs/radar-stage6-renderers-validators-2026-08-19.md`.
Реализованы explicit published-only `IssueDetail` projection, canonical JSON, byte-stable
dependency-free DOCX, явный no-LLM fallback, stats/duplicate/date/draft/security invariants,
независимая проверка JSON/OOXML и immutable gazette package validator. Golden fixtures и
historical acceptance покрывают обычный, fallback, полный no-LLM и пустой выпуски. Реальный
Stage 3 import также подтвердил совместимость с sparse Legacy material analysis и date-only
timestamps через узкий `legacy_inferred` compatibility boundary. На границе принятия Stage 6
publisher, activation и Stage 7 delta engine ещё не были реализованы.

## Этап 7. Delta engine и local publisher simulation

### Работы

1. Реализовать full seed export/import.
2. Реализовать row-level delta generation.
3. Реализовать upserts/tombstones.
4. Реализовать base release/hash checks.
5. Реализовать transaction apply к staging copy.
6. Реализовать state/table hashes.
7. Реализовать idempotent retries.
8. Реализовать publisher state machine.
9. Реализовать обязательный post-activation rollback с reload и проверкой previous release markers.
10. Прогнать source→disposable-production локально.

### Gate

- final logical state hashes source/target совпадают;
- per-table counts/hashes совпадают для полного replicated contract, включая drafts/queues/snapshots;
- duplicate apply безопасен;
- missing/out-of-order delta rejected;
- crash tests не повреждают active DB;
- full seed восстанавливает drift.

### Результат

Stage 7 завершён и зафиксирован в `docs/radar-stage7-delta-publisher-2026-08-19.md`.
Реализованы exact full-seed export/import, contract-v1 typed row delta с upserts/tombstones,
optimistic base/row fences, полный набор counts/hashes по 23 replicated tables, transactional
create-only staging apply и безопасный duplicate replay. Локальный publisher исполняет принятую
durable state machine для раздельных source/disposable-production roots, атомарно меняет только
малый active pointer, переоткрывает и проверяет release/state, восстанавливает previous pointer
после post-activation failure и блокирует новые публикации при недоказанном rollback. Candidate
replay связан с exact canonical delta/LLM/issue-date input и единой release/hash identity в journal.

Synthetic acceptance покрывает official delta/publisher-result schemas, SQL/path/security
отклонения, tombstones, correction, lock, parent-path swap, crashes до/после activation и оба
result/state crash window. На сохранённом реальном Stage 3 import full seed и correction delta
совпали по всем таблицам и logical state. Это всё ещё local disposable simulation: production,
cron, services, Caddy, DNS и Local Ru не изменялись.

## Этап 8. Реализовать read-only API и frontend V2

### Работы

1. Реализовать explicit public DTO layer.
2. Открывать SQLite только read-only.
3. Реализовать bounded validation всех query params.
4. Добавить OpenAPI и contract tests.
5. Реализовать frontend V2 поверх `/api`.
6. Реализовать no-LLM и empty issue UI.
7. Реализовать gazette index/view.
8. Добавить visual/security tests.
9. Реализовать safe release markers в `/api/health`.
10. Реализовать connection reopen/reload contract при content pointer switch.
11. Валидировать внешние URL по allowlist схем и экранировать их как HTML attributes.
12. Разделить SPA routes, real static assets и gazette paths, чтобы missing files давали 404, а не `index.html` с 200.

### Gate

- draft leakage tests green;
- path/internal metadata leakage отсутствует;
- malformed requests дают 4xx JSON;
- malicious URL schemes/attributes отклоняются, missing assets/gazettes дают 404;
- после pointer switch API возвращает ожидаемые release id/schema/state hash, а не старый inode;
- API/frontend work on disposable imported history;
- desktop/mobile/console smokes green.

### Результат

Stage 8 завершён и зафиксирован в
`docs/radar-stage8-readonly-api-frontend-2026-08-19.md`. Реализованы все 11 frozen OpenAPI routes,
pointer-aware `mode=ro&immutable=1` SQLite connection с обязательным release/state reopen,
published-view authorizer, bounded inputs/cursors/responses/search rate, exact JSON errors и
loopback-only stdlib HTTP transport. Три additive published-only view добавлены application-owned
миграцией 0002 без изменения таблиц или `user_version`.

Первоначальный dependency-free frontend реализовал latest/archive/issue/search/gazette и
responsive desktop/mobile layout. После уточнения владельцем целевого UI-контракта этот дизайн
сохранён только как rollback artifact: начальный публичный V2 frontend обязан быть визуально и
поведенчески идентичен актуальному Legacy baseline, используя при этом исключительно V2 API.
Security regressions продолжают покрывать HTTP(S)-only external links, draft/path/secret leakage,
SQL/PRAGMA и function denial, malformed inputs, traversal, missing assets, pointer switch и
immutable DB bytes.

Полный gate: 123 tests, Ruff, strict mypy, contracts, JavaScript, console smoke, isolation/secret
scan и reproducible artifact PASS. Свежая копия реального исторического импорта прошла полный API
matrix на 74 выпусках; известные Legacy date anomalies допускаются только при согласованном
quality delta и явном `medium|high/queued`, native V2 остаётся строгим. Production/Legacy, cron,
services, Caddy, DNS и Local Ru не менялись.

## Этап 9. Application release automation

### Работы

1. Создать immutable application artifact.
2. Включить commit/tag provenance.
3. Добавить compatibility manifest.
4. Добавить source/production deploy scripts.
5. Добавить atomic symlink activation.
6. Добавить dependency-ordered rollback.
7. Реализовать application/migration lock, взаимоисключающий с content publisher.
8. Реализовать одинаковые versioned migrations на source/production staging copies и coordinated rollback.
9. Application deploy оставить manual-approved.

### Gate

- artifact reproducible;
- content candidate не может изменить schema; schema migration возможна только в явно подтверждённом application release;
- migration rehearsal сохраняет одинаковую source/production schema и откатывает обе стороны при ошибке;
- production artifact excludes publisher/OpenClaw/LLM code where not needed;
- local staging deploy/rollback green.

### Результат

Stage 9 завершён и зафиксирован в
`docs/radar-stage9-application-release-2026-08-19.md`. Clean commit
`d45069d8639019da02bfb7927484d32d7c327331` собран в побайтово воспроизводимый release
`app_release_20260819_d45069d`; внешний package SHA-256 —
`81b7c26802c6f82e23ae8f502366405a610b810eb4c8c498a3dc630a882eee78`.

API, web и migration bundle разделены, хешированы и связаны strict compatibility/provenance
manifest. Public API artifact содержит только published read path и исключает publisher,
candidate builder, editorial/LLM orchestration, OpenClaw, tests и fixtures. Application deployment
и content publisher используют общий no-follow `radar-mutation.lock`; content candidate по-прежнему
не может передавать SQL/DDL/migrations.

Test-only local rehearsal независимо мигрировала source/production staging copies миграциями
`0001 -> 0002`, получила одинаковый schema SHA-256
`5c7e6e66afc7fd814f25c5bb7b441e22131db8ffc35cf00fd2d81760ccbc6266`, выполнила dependency-ordered
activation, доказанный rollback и повторную активацию. Полный gate: 142 tests, Ruff, strict mypy,
contracts, JavaScript, console smoke, isolation/secret scan и 21-file public artifact PASS.
Legacy/production, services, cron, Caddy, DNS и Local Ru не менялись.

## Этап 10. Подготовить Local Ru без public activation

### Работы

1. Проверить disk/RAM/ports/Caddy/UFW/current NRD load.
2. Создать dedicated users/groups.
3. Создать release/data/incoming paths.
4. Установить hardened systemd API unit.
5. Подготовить restricted remote activator identity/quarantine; SSH transport и forced entrypoint
   устанавливать только вместе с tested publisher integration на Stage 13.
6. Развернуть application release без public DNS.
7. Проверить loopback API и filesystem permissions.
8. Не менять основной Radar DNS.

### Gate

- service non-root;
- API loopback-only;
- systemd security review приемлем;
- no external internal-port exposure;
- NRD services не деградировали;
- reboot persistence проверена позднее отдельным stage.

### Read-only preflight

Read-only аудит зафиксирован в `docs/radar-stage10-local-ru-preflight-2026-08-19.md`. На Local Ru
достаточно RAM/disk/inodes, порты 8765–8767 свободны, UFW deny-by-default, Caddy valid/active и все
внутренние NRD ports loopback-only/externally closed. Все семь canonical NRD units active+enabled,
`NRestarts=0`, `/api/health` green, error-priority journal rows отсутствуют. Radar users, paths,
units и vhost ещё не существуют; удалённых изменений не выполнялось.

До deploy выявлен обязательный runtime blocker: target имеет Python 3.14.4/SQLite 3.46.1, а contract
требует exact Python 3.12.3/SQLite 3.45.1 profile. Системный Python менять нельзя; нужен отдельный
immutable Radar runtime под `/opt/radar-v2-runtime`, его portability/hash/rollback proof и отдельное
явное подтверждение установки. Hardened clean candidate `app_release_20260819_545bf2e` (commit
`545bf2e11db924b0bacf3b5ac71092495fd8052b`, package SHA-256
`85accde8b8c77c1fb8d10e84c267be77e7ca7af8e7fdc7e24e3dfcee02a727eb`) прошёл полный gate и local
rollback rehearsal; на Local Ru не передавался и не устанавливался.

### Результат

После явного подтверждения владельца Stage 10 завершён и зафиксирован в
`docs/radar-stage10-local-ru-loopback-2026-08-20.md`. На Local Ru установлены отдельный
побайтово воспроизводимый runtime CPython 3.12.3/SQLite 3.45.1, immutable application release
`app_release_20260819_545bf2e`, locked identities `radar-v2-api`/`radar-v2-deploy`, private
release/data/incoming/audit paths и hardened `radar-v2-api.service`. Deploy identity/quarantine
готовы, но SSH key/forced command/transport намеренно не установлены до Stage 13.

Активирован только пустой schema release `content_release_stage10_empty`: source/target staging
SQLite побайтово совпадают, все domain tables пусты, integrity/FK/schema/table hashes green. API
active+enabled, non-root, `NRestarts=0`, слушает только `127.0.0.1:8765`, systemd security `2.7 OK`,
filesystem write-open блокируется `EROFS`, 14-route loopback matrix green, внутренние порты снаружи
закрыты. Все семь NRD units и Caddy сохранили active+enabled/`NRestarts=0`, UFW/Caddy/DNS и Legacy
production не изменены. Исторический seed, public Caddy/DNS, publisher transport и cron остаются
за границей Stage 11+.

## Этап 11. Initial seed и историческая acceptance

### Работы

1. Построить verified V2 source DB из Legacy history.
2. Выполнить full seed на Local Ru.
3. Сравнить schema и per-table row counts/hashes для **каждой** replicated table.
4. Запустить API на loopback.
5. Выполнить historical endpoint parity checks.
6. Проверить physical presence и точное совпадение drafts, manual/deferred/review queues и snapshot metadata.
7. Проверить public invisibility drafts/queues.
8. Проверить historical correction в disposable/shadow context.

### Gate

- source/production logical state hashes совпадают;
- table inventory, row counts и hashes совпадают по полному replicated contract;
- все исторические выпуски доступны;
- drafts/queues присутствуют в DB и не доступны публично;
- correction + rollback rehearsal green.

### Результат

После явного подтверждения владельца Stage 11 завершён и зафиксирован в
`docs/radar-stage11-initial-seed-2026-08-20.md`. Frozen Legacy inputs детерминированно импортированы
в V2 release `rel_e404ff802c3e3c71083529ed`: full seed SHA-256
`5970470c28db4998b07d21052e196e97e55c5cb0ddbd60e4671e0a5861ea54d9`, logical state
`ef5b4c3ef7ddfcda05c5aad331043bcc576ec641683e05d74ce1162e1e7c7f41`.

Source/export/import/Local Ru совпали по inventory, row count и logical hash всех 23 replicated
tables. Loopback API дал historical parity для 74 выпусков и 254 material relations; 128 queue
rows, 26 unassigned materials, Legacy provenance и snapshot metadata физически присутствуют, но
не видны через public DTO. Disposable correction доказала success и forced-smoke rollback.

Живой content pointer на Local Ru прошёл Stage 11 full-seed -> Stage 10 empty -> Stage 11
re-activation без service restart: PID `23919`, `NRestarts=0`, exact database hashes сохранены.
Во время первого rehearsal найден и устранён fail-closed дефект операторского скрипта: новый
pointer inode получил неверный owner `root:root`; после восстановления
`radar-v2-api:radar-v2-api 0600` health вернулся без рестарта, исправленный rehearsal green. Для
Stage 13 закреплён обязательный инвариант сохранения bytes/UID/GID/mode/link count при atomic
pointer switch.

Caddy/DNS, publisher transport, cron и reboot не затрагивались. `radar.aipractice.space` остаётся
на Legacy `72.56.107.196`; Local Ru V2 остаётся loopback-only.

## Этап 12. Shadow hostname и HTTPS

### Работы

1. Выбрать и настроить V2 hostname.
2. Добавить отдельный Caddy vhost.
3. Получить TLS certificate.
4. Подключить frontend/API/gazette.
5. Проверить security headers/cache/CSP.
6. Проверить public smokes и logs.

### Gate

- HTTPS green;
- SPA/API routing isolated;
- internal ports закрыты;
- Legacy hostname не изменён.

## Этап 12A. Legacy frontend parity на V2 backend

### Целевой контракт и роли

- `radar.aipractice.space` остаётся независимым Legacy production; его frontend, backend и
  pipeline продолжает дорабатывать Project Manager.
- `radar.agpm.space` использует V2 backend и immutable application releases, но его начальный
  frontend должен быть визуально и поведенчески идентичен зафиксированному Legacy baseline.
- После Stage 12A frontend и backend V2 изменяются coding-агентами через Git, тесты и immutable
  release. Изменения Project Manager в Legacy не копируются в V2 автоматически: каждый перенос
  является отдельной осознанной coding-agent change с parity/regression gate.
- Legacy и V2 существуют параллельно. Cutover, удаление или отключение Legacy требуют отдельного
  решения владельца.

### Работы

1. Зафиксировать exact Legacy HTML/CSS/JS/font/favicon/social baseline и его hashes.
2. Перенести Legacy layout, widgets, responsive rules и interactions в V2 web artifact.
3. Адаптировать только data boundary: все данные V2 frontend получает через frozen published V2
   API; Legacy API/SQLite/runtime не входят в release и не вызываются из браузера.
4. Сохранить предыдущий самостоятельный V2 frontend как immutable rollback application release.
5. Добавить DOM/asset, desktop/mobile, console, CSP/static-route и external-URL regressions.
6. Собрать clean immutable application release, активировать его на Local Ru с доказанным
   rollback и выполнить public parallel acceptance против Legacy.

### Gate

- desktop/mobile структура, стили, widgets и interactions соответствуют baseline;
- favicon, social image и шрифты совпадают по SHA-256;
- browser network не обращается к Legacy hostname/API;
- V2 public API, private/unknown 404 boundary, CSP/cache и loopback isolation green;
- Legacy code/data/services/Caddy/DNS не изменены;
- предыдущий V2 application release сохранён и rollback доказан.

### Результат

Stage 12A завершён и зафиксирован в
`docs/radar-stage12a-legacy-frontend-parity-2026-08-20.md`. Legacy layout/widgets/responsive
behavior, точные шрифты, favicon/social assets и газета перенесены в V2 web artifact; browser data
adapter использует только published V2 API. Desktop/mobile DOM/widget parity, console/network,
public routes, CSP, isolation и rollback приняты. Активен immutable release
`app_release_20260820_10fc9c8`; предыдущий самостоятельный V2 frontend сохранён как rollback.
Legacy production и три изменения Project Manager не затронуты.

## Этап 13. Интеграция publisher с Local Ru

### Работы

1. Подключить dedicated SSH transport.
2. Передать test delta.
3. Проверить incoming quarantine.
4. Выполнить remote staging apply.
5. Проверить atomic activation и обязательный API reload/reopen; pointer switch обязан сохранять
   и проверять exact bytes, UID, GID, mode, link count и fsync durability.
6. Проверить ожидаемые release id/schema/state hash через loopback и public API.
7. Проверить structured publisher result.
8. Rehearse transfer failure, base mismatch, loopback failure, public smoke failure, source commit failure и rollback.
9. Rehearse rollback verification failure и блокировку дальнейших releases через `NEEDS_RECONCILIATION`.

### Gate

- no live DB in-place mutation;
- source/production logical state hashes converge;
- любая post-activation ошибка возвращает previous remote release, reloads API и подтверждает previous markers;
- если rollback нельзя подтвердить, дальнейшие publications заблокированы;
- publisher final result unambiguous.

### Фактический результат 2026-08-20

Stage 13 завершён. Dedicated restricted SSH/forced-command transport, private quarantine,
create-only remote staging, exact pointer activation, loopback/public verification and explicit
remote rollback установлены и приняты. Успешная private-only delta свела source и Local Ru к
`rel_stage13_private_transport_01` / `2c6e1ba75b252a8de5a2e0a0413bd31d6aa50968ebee228522a61cd9da30bff6`.
Transfer/base/loopback/public/source-commit/rollback-proof failure branches прошли; непроверяемый
rollback переводит publisher в `NEEDS_RECONCILIATION` и блокирует следующие запросы. Полный отчёт:
`docs/radar-stage13-publisher-local-ru-2026-08-20.md`.

## Этап 14. Project Manager end-to-end dry runs

### Работы

1. Подключить V2 candidate flow к Project Manager без cron activation.
2. Выполнить manual daily run.
3. Выполнить no-LLM run.
4. Выполнить historical duplicate correction.
5. Выполнить gazette create/update.
6. Проверить user-facing final reports.
7. Не менять Legacy cron.

### Gate

- все три user scenarios end-to-end green;
- Project Manager не имеет прямого production SQL path;
- каждый run заканчивается сообщением пользователю;
- retry не дублирует публикацию.

## Этап 15. Включить ежедневный dual-run

### Работы

1. Backup cron/config перед изменением.
2. Сохранить Legacy branch и schedule semantics.
3. Добавить immutable snapshot/fork.
4. После Legacy выполнять V2 candidate/publish.
5. Добавить combined comparison report.
6. Добавить timeout isolation: V2 failure не блокирует Legacy result.
7. Наблюдать daily runs.
8. Изменять cron через активный Project Manager OpenClaw instance/state, а не через мигрированные `cron/jobs.json*`.
9. Сохранить доказанный внешний порядок: build/validate DOCX, успешная delivery, Legacy publish, V2 publish, combined final report.
10. Backup должен включать cron row/payload/state из активного OpenClaw SQLite store и читаемый экспорт job до изменения.

### Gate

- минимум согласованного числа последовательных успешных дней;
- Legacy behaviour unchanged;
- V2 publishes automatically;
- no-LLM fallback проверен хотя бы fixture/controlled run;
- drift/reconciliation green;
- пользователь может сравнивать оба frontend.

Срок dual-run не ограничен: переход дальше только по явному решению пользователя.

### Фактический результат 2026-08-20

Stage 15 завершён для согласованной цели выпуска 20 августа. Legacy cron сохранён без изменений;
в активном Project Manager state добавлен отдельный enabled post-Legacy cron на 08:25 МСК. Он
создаёт immutable snapshot/fork, публикует отсутствующий V2 daily candidate и всегда формирует
combined comparison с timeout isolation от Legacy. Scheduler-run для 2026-08-20 завершился `ok`,
Telegram delivery прошла, а уже опубликованная дата была обработана как `already_published` без
дублирования release. Канонический отчёт:
`docs/radar-stage15-project-manager-dual-run-2026-08-20.md`.

## Этап 16. Reboot и disaster-recovery rehearsal Local Ru

### Работы

1. Проверить enabled units и tmpfiles/permissions.
2. Выполнить согласованный reboot.
3. Проверить API/Caddy/NRD после reboot.
4. Rehearse application rollback.
5. Rehearse DB release rollback.
6. Rehearse full seed recovery.
7. Rehearse source/production reconciliation.

### Gate

- reboot recovery green;
- rollback documented and timed;
- no NRD regression;
- no external internal-port exposure.

### Фактический результат 2026-08-20

Stage 16 завершён после явного подтверждения владельца. Local Ru пережил согласованный reboot;
Radar API, Caddy и семь NRD units автоматически восстановились enabled/active с `NRestarts=0`.
Application rollback, DB release rollback/re-activation, create-only full-seed recovery и полная
source/production/recovered-seed reconciliation прошли. Перед Stage 16 исправлена согласованность
отфильтрованного daily narrative и опубликована sequence 9. Канонический отчёт:
`docs/radar-stage16-local-ru-dr-2026-08-20.md`.

По решению владельца Stage 16 является финальной границей текущего плана. Этапы 17–19 и cutover
не входят в эту приёмку; оба стенда остаются в наблюдении.

## Этап 17. Пользовательская приёмка V2

Пользователь сравнивает:

- состав выпусков;
- старые и новые правила;
- дубли;
- даты;
- LLM/fallback;
- historical corrections;
- газеты;
- frontend desktop/mobile;
- стабильность daily publishing.

Все замечания исправляются в V2 через обычную разработку и application/content release flows. Legacy не переделывается.

### Gate

Только явная команда владельца: V2 принят и разрешён cutover.

## Этап 18. Cutover основного домена

### Работы

1. Зафиксировать current verified application/content/gazette releases.
2. Выполнить final source/production reconciliation.
3. Сделать backups обоих серверов и DNS state.
4. Подготовить emergency DNS rollback.
5. Переключить `radar.aipractice.space` на Local Ru.
6. Получить/проверить certificate.
7. Выполнить public API/frontend/gazette smoke.
8. Проверить Project Manager следующий publish через основной домен.
9. Legacy оставить доступным по отдельному rollback/legacy hostname либо старому IP по согласованной схеме.

### Gate

- authoritative/public DNS указывает Local Ru;
- HTTPS/API/frontend green;
- Project Manager publication green;
- monitoring/logs green;
- rollback path сохранён.

## Этап 19. Observation и завершение миграции

### Работы

1. Наблюдать production agreed period.
2. Сравнивать source/production logical state hashes ежедневно.
3. Проверить минимум один correction после cutover.
4. Проверить следующий gazette release.
5. Документировать финальный production baseline.
6. Не удалять Legacy, backups или migration artifacts без отдельного разрешения.

### Gate

- пользователь явно подтверждает завершение миграции;
- только после этого Legacy может быть отдельно выведен из эксплуатации.

## 24. Cutover acceptance checklist

До переключения DNS должны быть выполнены все пункты:

- [ ] Legacy продолжает работать;
- [ ] V2 application release reproducible;
- [ ] source/production SQLite schema одинаковы;
- [ ] full seed проверен;
- [ ] delta idempotency проверена;
- [ ] tombstones проверены;
- [ ] correction overwrite проверен;
- [ ] no-LLM publication проверена;
- [ ] draft leakage tests green;
- [ ] historical import принят;
- [ ] gazette create/update принят;
- [ ] Project Manager final reporting принят;
- [x] Local Ru reboot green;
- [x] application rollback green;
- [x] DB rollback green;
- [x] reconciliation/full reseed green;
- [x] NRD на Local Ru не деградировал;
- [ ] user explicitly approved cutover.

## 25. Запрещённые сокращения пути

Нельзя:

- копировать текущую live SQLite напрямую на Local Ru как постоянный deploy mechanism;
- применять delta к active DB inode;
- давать API write permissions «на всякий случай»;
- публиковать `SELECT *` из внутренних таблиц;
- считать endpoint `/api/internal/*` безопасным только из-за названия;
- хранить drafts на production без тестов public filtering;
- смешивать schema migration и произвольный content SQL;
- давать package возможность передавать SQL;
- использовать root как runtime API user;
- ставить V2 cron до end-to-end dry runs;
- менять Legacy правила ради удобства V2;
- переключать DNS без явного подтверждения пользователя;
- удалять Legacy или rollback releases в рамках миграции.

## 26. Первый следующий шаг

Stage 0, urgent Stage 0A и Stage 1–16 завершены. По явному решению владельца текущий план на этом
выполнен; Stage 17–19, cutover и вывод Legacy из эксплуатации не авторизованы. Следующий режим —
наблюдение обоих стендов и ежедневного dual-run.

Stage 12 опубликован как изолированный shadow по `https://radar.agpm.space` на Local Ru
`147.45.99.225`. Authoritative и публичный DNS, Let's Encrypt TLS, HTTP redirect, frontend/API,
security headers, private/unknown 404 boundary и loopback-only API приняты. Caddy и Radar API
сохранили PID и `NRestarts=0`; все семь NRD units и публичный NRD health green.

Stage 14 провёл manual daily, no-LLM, historical correction и gazette update через restricted
transport, подтвердил идемпотентный retry и финальные Project Manager reports. Основной
`radar.aipractice.space`, Legacy cron и cutover не менялись. Канонический отчёт:
`docs/radar-stage14-project-manager-dry-runs-2026-08-20.md`.
