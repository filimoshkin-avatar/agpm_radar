# Миграция KX 003: провенанс, публикация и путь импорта артефактов

Дата: 2026-08-22. Срез 1.3 плана `docs/radar-v2-kb-engineering-plan-2026-08-22.md`.

Статус: **написана, проверена вне production, к production не применялась.** Применение миграции и
переключение `/opt/radar-kx/current` требуют отдельного подтверждения владельца (§16 плана).

---

## 1. Что добавляет миграция

`kx/sql/003_provenance_and_publication.sql`.

| Объект | Зачем |
|---|---|
| Расширение таксономии `source_kind` | Ступени лестницы добычи, у которых не было имени: `network_browser_headers`, `browser_render`, `web_archive`, `local_import`. Плюс `operator_artifact`, который в production попал хотфиксом без миграции (D1) |
| `version_provenance` (append-only) | Как именно получены байты версии. Идентичность версии не включает способ добычи, поэтому неверный `source_kind` нельзя исправить новой версией — только записью рядом (D12) |
| `version_provenance_current` | Последняя запись провенанса на версию: что мы считаем правдой сейчас |
| `version_publication_block` | Fail-closed вход публикации: версия цитируема, только если провенанс записан и полон. Отсутствие провенанса блокирует так же громко, как плохой провенанс |
| `source_publication_policy` | Формулировка атрибуции на источник и, при необходимости, более короткий предел цитаты (P32/P34) |
| `egress_audit` | Каждый вызов внешней модели: что ушло, сколько, кому, каким прогоном (P18, P30) |
| `wiki_blobs`, `wiki_snapshots`, `wiki_snapshot_files` | Снимок файловой wiki: манифест per-file SHA-256 плюс content-addressed блобы (P27) |
| `store_reconciliation_reports` | Регулярная сверка файлового хранилища с KX (P28) |
| `corpus_imports.source_kind` | Класс членства корпуса: `radar_materials`, `canon_import`, `operator_import` |
| `documents_canonical_url_scheme` | Резервирование схемы `agpm-canon:/` для канона, у которого нет веб-адреса |
| `schema_version = 3` | Жёсткий гейт `require_schema` (D2) |

Все новые таблицы, кроме `source_publication_policy`, immutable по триггеру
`reject_immutable_mutation`. Политика публикации — редакционное решение, которое пересматривается;
она хранит `decided_by` и `decided_at`.

### 1.1. Почему появилась ступень `local_import`

План перечислял три новые ступени. Четвёртая нужна срезу 1.6: канон AgPM загружается из локальных
markdown-файлов `agpm/raw/`, которые никто не запрашивал по сети и никто не передавал как
операторский артефакт. Записать их как `operator_artifact` значило бы внести неправду в
доказательную базу — ровно тот класс ошибки, который создал D9. Одно значение перечисления стоит
дешевле, чем неверная запись о происхождении канона.

---

## 2. Восстановленный путь импорта

`radar_kx import-artifact --manifest <файл>` — ступень 7 лестницы (§11.6) и единственный инструмент
среза 1.6. Реализация: `kx/src/radar_kx/artifact_import.py`.

**Ключевое отличие от `store_cached_version`: строка `fetch_attempts` не пишется.** Сетевого запроса
не было, и синтетический HTTP 200 для файла — это ровно то, как две обычные browser-header загрузки
оказались в базе помеченными как операторский артефакт (D9).

Манифест обязан нести провенанс на каждый документ. Отказы:

| Ситуация | Поведение |
|---|---|
| Запись без `provenance` | Отказ: «a file with no recorded origin cannot be imported» |
| `source_access_method` из сетевых (`http_default`, `browser_headers`, `robots_override`, `browser_render`) | Отказ: файл не может утверждать запрос, которого не было |
| `web_archive` без `archive_url` и `archive_captured_at` и без `manual_review_required` | Отказ: правило 19 неисполнимо |
| `path` выходит за каталог манифеста | Отказ |
| Один канонический URL дважды | Отказ |
| Файл разбирается в пустой текст | Отказ: «metadata is not full text» |
| Файл разбирается в короткий текст | Импортируется как **не complete**: не становится `best_version_id`, очередь не закрывается |
| Повторный импорт того же файла | Идемпотентен: версия не дублируется, строка провенанса не дублируется |

`radar_kx record-provenance --file <файл>` дописывает провенанс к уже существующим версиям.
Идемпотентность на уровне содержания: если последняя запись провенанса версии совпадает по всем
полям, новая не добавляется — append-only не означает append-duplicates.

**Запрет на уровне кода.** `Database.record_fetch_result` отклоняет любой `source_kind` вне
`NETWORK_SOURCE_KINDS`, `store_artifact_version` — любой вне `ARTIFACT_SOURCE_KINDS`. Сетевой запрос
больше не может быть записан как операторский артефакт.

---

## 3. Что уже проверено вне production

`kx/scripts/verify_migrations.sh` поднимает временную базу и прогоняет 33 теста. Проверено:

- миграция ложится на **репозиторную** базу схемы 2;
- миграция ложится на **дрейфовую** базу — схема 2 плюс хотфикс `operator_artifact`, применённый
  руками 2026-08-22 (D1);
- обе базы после миграции **совпадают по колонкам** (`information_schema.columns` идентичен);
- новые ступени принимаются, выдуманная — отвергается;
- провенанс append-only: UPDATE и DELETE падают на триггере;
- архивный снимок без URL и даты снимка отвергается, если не помечен `manual_review_required`;
- `version_publication_block` блокирует и версию без провенанса, и версию с пометкой на ревизию;
- схема `agpm-canon:/` принимается, `file://` и пустой путь — нет;
- у роли `radar_kx` есть права на все новые таблицы, вьюхи и последовательности;
- импорт артефакта не пишет `fetch_attempts`, повторный импорт ничего не меняет, метаданные не
  становятся полным текстом.

Основной гейт `kx/scripts/verify.sh` проходит целиком: 106 тестов, 22 пропущено (это как раз
SQL-тесты, которым нужен сервер).

---

## 4. Состояние production на 2026-08-22

Проверено read-only.

| Факт | Значение |
|---|---|
| `schema_version` | 2 |
| Дрейф D1 | присутствует: `operator_artifact` есть в обоих constraint'ах |
| `radar-kx-ingest.timer` | **enabled, но inactive** — остановлен 2026-08-21 18:16 UTC |
| Последний прогон ingest | 2026-08-21 17:45 UTC, завершился успешно |
| `radar-kx-backup.timer` | active, `OnCalendar=*-*-* 02:20:00 UTC` |
| Текущий релиз | `/opt/radar-kx/current` → `releases/radar_kx_release_20260821_issue_perimeter_cb2bd80f030c` |
| Версий `operator_artifact` | 25 (23 `operator_artifact_html`, 2 `trafilatura`) |

**Отдельная находка, не относящаяся к миграции: ingest KX не работает почти сутки.** Таймер
остановлен 21 августа в 18:16 UTC и не возвращён. В очереди 2 378 failed, и ретраев не происходит.
Это не ломает периметр (275/275 полных текстов на месте) и не мешает миграции — но состояние надо
восстановить сознательно, а не обнаружить потом. Требуется решение владельца: возвращать таймер
сейчас, после миграции или оставить остановленным.

---

## 5. Runbook применения

**Не выполнять без подтверждения владельца.** Порядок «база, потом релиз» обязателен: `SCHEMA_VERSION`
в `database.py` — жёсткий гейт (D2), поэтому развёрнутый сейчас релиз перестанет работать в момент
подъёма версии схемы, а новый релиз не работает до него.

Окно: **не** между 02:10 и 02:40 UTC — это окно `radar-kx-backup.timer`.

```bash
# 0. Зафиксировать состояние до изменения.
systemctl is-active radar-kx-ingest.timer radar-kx-backup.timer > /root/kx-003-pre-state.txt
sudo -u postgres psql -d radar_kx -X -qAt -c \
  "select value from kx.metadata where key='schema_version'"

# 1. Остановить таймеры. Интервал ingest - 30 минут; запуск во время миграции
#    упадёт по require_schema.
systemctl stop radar-kx-ingest.timer
systemctl stop radar-kx-backup.timer
systemctl is-active radar-kx-ingest.service   # должно быть inactive

# 2. Ручной backup вне окна backup-таймера.
stamp="$(date -u +%Y%m%dT%H%MZ)"
install -d -m 0700 "/var/backups/radar-kx/manual-${stamp}"
sudo -u postgres pg_dump --format=custom --file="/var/backups/radar-kx/manual-${stamp}/radar_kx_pre_003.dump" radar_kx
sha256sum "/var/backups/radar-kx/manual-${stamp}/radar_kx_pre_003.dump"
pg_restore --list "/var/backups/radar-kx/manual-${stamp}/radar_kx_pre_003.dump" > /dev/null && echo "restore list ok"

# 3. Применить миграцию. ON_ERROR_STOP плюс единственная транзакция внутри файла:
#    либо схема 3 целиком, либо ничего.
sudo -u postgres psql -d radar_kx -v ON_ERROR_STOP=1 -f /opt/radar-kx/current/sql/003_provenance_and_publication.sql

# 4. Проверить версию.
sudo -u postgres psql -d radar_kx -X -qAt -c \
  "select value from kx.metadata where key='schema_version'"      # ожидается 3

# 5. Переключить указатель релиза на сборку с SCHEMA_VERSION = 3.
ln -sfn "releases/<новый релиз>" /opt/radar-kx/current.new && mv -T /opt/radar-kx/current.new /opt/radar-kx/current

# 6. Проверить связку.
sudo -u radar_kx /opt/radar-kx/runtime/current/bin/python -m radar_kx verify --full

# 7. Вернуть таймеры в то состояние, которое зафиксировал шаг 0.
systemctl start radar-kx-backup.timer
# ingest - только если он был активен до изменения (сейчас он не активен).
```

### 5.1. Откат

Миграция целиком в одной транзакции, поэтому частично применённого состояния не бывает. Откат:

```bash
systemctl stop radar-kx-ingest.timer radar-kx-backup.timer
ln -sfn "releases/radar_kx_release_20260821_issue_perimeter_cb2bd80f030c" /opt/radar-kx/current.new
mv -T /opt/radar-kx/current.new /opt/radar-kx/current
sudo -u postgres pg_restore --clean --if-exists --dbname=radar_kx \
  "/var/backups/radar-kx/manual-${stamp}/radar_kx_pre_003.dump"
```

Откат «вниз по схеме» без восстановления дампа не предусмотрен намеренно: DROP новых таблиц уничтожил
бы append-only записи, а они и существуют затем, чтобы их нельзя было потерять.

---

## 6. Backfill провенанса 25 версий

Выполняется **после** миграции, отдельным шагом, тоже с подтверждением.

Данные: `kx/data/provenance-backfill-2026-08-22.json`, 25 записей. Источники —
`/root/.openclaw/workspace/reports/radar-kx-fulltext-close-2026-08-22.md` (что было импортировано) и
`.../radar-kx-fulltext-coverage-2026-08-22.md` (откуда взят текст каждого документа).

```bash
sudo -u radar_kx /opt/radar-kx/runtime/current/bin/python -m radar_kx \
  record-provenance --file /opt/radar-kx/current/data/provenance-backfill-2026-08-22.json
```

Ожидаемый результат: `appended: 25`, `unchanged: 0`, `documentsNotInStore: []`. Повторный запуск даёт
`appended: 0`, `unchanged: 25`.

Разбивка:

| Ступень | Документов | Что записано |
|---|---:|---|
| `operator_file` | 19 | HTML-артефакт владельца; `provided_by`, `provided_at`, `original_url` |
| `web_archive` | 4 | adopt.ai и три материала McKinsey; `manual_review_required = true` |
| `browser_headers` | 2 | Appian и Futurum — коррекция D9 |

### 6.1. Четыре документа остаются заблокированными для публичного цитирования

adopt.ai и три McKinsey фактически взяты из веб-архива, но URL снимка и дата снимка не были
записаны. Правило 19 (§8.6) для них неисполнимо: цитата обязана указывать на тот снимок, из которого
взята. После backfill они попадают в `version_publication_block` с причиной
`provenance_manual_review` и **не могут быть процитированы публично**, пока снимок не восстановлен.

Это принимается как временное ограничение, а не как ошибка. Восстановление — работа среза 2.3
(клиент веб-архива): найти снимок, сверить текст, дописать `archive_url` и `archive_captured_at`
новой записью провенанса. Блокировка снимется сама, потому что она вычисляется из провенанса, а не
выставляется руками.

### 6.2. Коррекция D9

Версии Appian и Futurum записаны с `source_kind = 'operator_artifact'`, хотя в `fetch_attempts` по
тем же URL лежит успешная попытка `http_status = 200` с пояснением «direct browser-header HTTP
fetch». Новые версии для исправления невозможны: `version_id` не покрывает способ добычи, и вставка
столкнулась бы с PRIMARY KEY и UNIQUE (D12). Запись коррекции говорит правду, не трогая ни версию,
ни immutable-строку попытки.

---

## 7. Чего этот срез не делает

- Не применяет миграцию к production и не переключает `/opt/radar-kx/current`.
- Не создаёт и не изменяет ни одного systemd-юнита, таймера, правила Caddy, DNS или cron.
- Не пишет ничего в production-базы.
- Не загружает канон AgPM — это срез 1.6, который опирается на `import-artifact` и
  `source_kind = 'canon_import'` отсюда.
- Не строит клиент веб-архива и не восстанавливает четыре архивных снимка — срез 2.3.
- Не добавляет `wiki_edit_journal` (P37): его нет в составе среза 1.3, и он появится вместе с
  контуром снимков wiki.
- Не исправляет D3 (view `issue_perimeter_documents` не фильтрует по `perimeter_source_id`):
  расхождение латентно, правило обращения записано в corpus-membership contract §6.
