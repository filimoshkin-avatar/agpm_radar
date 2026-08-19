# Radar V2: архитектура, пересборка и миграция production на Local Ru

Дата исходного плана: 2026-08-19

Дата полной переработки: 2026-08-19

Статус: согласованный master plan; реализация ещё не начата

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

## Этап 3. Реализовать V2 SQLite и Legacy importer

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

## Этап 10. Подготовить Local Ru без public activation

### Работы

1. Проверить disk/RAM/ports/Caddy/UFW/current NRD load.
2. Создать dedicated users/groups.
3. Создать release/data/incoming paths.
4. Установить hardened systemd API unit.
5. Установить restricted remote activator.
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

## Этап 13. Интеграция publisher с Local Ru

### Работы

1. Подключить dedicated SSH transport.
2. Передать test delta.
3. Проверить incoming quarantine.
4. Выполнить remote staging apply.
5. Проверить atomic activation и обязательный API reload/reopen.
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
- [ ] Local Ru reboot green;
- [ ] application rollback green;
- [ ] DB rollback green;
- [ ] reconciliation/full reseed green;
- [ ] NRD на Local Ru не деградировал;
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

Stage 0, urgent Stage 0A и Stage 1 завершены. Следующий последовательный шаг — **Stage 2: изолированный Radar V2 repository skeleton**.

Stage 2 обязан сделать contract generator/validator обязательным local/CI gate, закрепить exact SQLite build profile и не подключаться к Legacy cron/runtime. Stage 3 использует frozen 74-issue evidence manifest; Stage 5 реализует closed desired-state candidates; Stage 7 принимает только generated typed deltas с full replicated-table expectations.

До завершения Stage 2 не создаются Local Ru Radar services, не меняется Project Manager cron, не меняется DNS и не переносится production data.
