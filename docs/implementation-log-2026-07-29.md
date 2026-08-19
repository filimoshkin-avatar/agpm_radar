# Реализация Radar: статус на 2026-07-29

## Что сделано

1. Создана рабочая структура проекта на `/mnt/vdd/Radar`.
2. Синхронизирован корпус `knowledge/agpm-radar/` в `/mnt/vdd/Radar/data/corpus/knowledge-agpm-radar/`.
3. Скопированы текущие скрипты радара в `/mnt/vdd/Radar/pipeline/scripts/` без изменения старого workspace.
4. Скопированы исторические DOCX-выпуски в `/mnt/vdd/Radar/data/corpus/raw-docx/`.
5. Создана SQLite-база `/mnt/vdd/Radar/data/db/radar.sqlite`.
6. Добавлена миграция `/mnt/vdd/Radar/data/db/migrations/001_initial.sql`.
7. Реализован backfill-скрипт `/mnt/vdd/Radar/pipeline/scripts/agpm_radar_docx_backfill.py`.
8. Реализован русскоязычный классификатор `/mnt/vdd/Radar/pipeline/scripts/agpm_radar_llm_classify.py` с OpenAI-compatible режимом и fallback-правилами.
9. Реализован JSON-cache экспортёр `/mnt/vdd/Radar/pipeline/scripts/agpm_radar_site_export.py`.
10. Реализован первичный backend API `/mnt/vdd/Radar/backend/radar-api/server.py`.
11. Создан лёгкий frontend-скелет `/mnt/vdd/Radar/work/radar-app/`, читающий данные из локального API.
12. Собран ежедневный pipeline `/mnt/vdd/Radar/pipeline/bin/radar_daily_publish.sh`.

## Текущие данные

- Выпусков в SQLite: 53.
- Материалов в SQLite: 121.
- Нумерация выпусков: с 2026-06-07 как № 1; 2026-07-29 — № 53.
- Для выпуска 2026-07-29: просмотрено 64, включено 13, отсечено 51; близкий периметр — 2, средний — 11, дальний — 0.
- Полный сетевой metadata-backfill выполнен по историческим ссылкам.
- `source_metadata`: 63 `resolved`, 13 `low_confidence`, 42 `unresolved`.
- `materials.publication_date_status`: 64 `resolved`, 49 `low_confidence`, 8 `unresolved`.
- Найдены материалы, у которых дата публикации существенно раньше даты выпуска радара; например материал из июньского/июльского выпуска может быть сохранён с датой публикации 2025 года или начала 2026 года.
- JSON-кэш создан в `/mnt/vdd/Radar/data/exports/json-cache/`.

## Локальная проверка

Запущены dev-процессы:

- backend API: `http://127.0.0.1:8765/api/health`;
- frontend-скелет: `http://127.0.0.1:8780/`.

Проверено:

- `/api/health` отдаёт 200;
- `/api/latest` отдаёт выпуск 2026-07-29, № 53, 13 материалов;
- `/api/rubrics` отдаёт 11 рубрик;
- `/api/sources` отдаёт источники;
- frontend HTML отдаёт 200 и читает API через CORS.

## Важные ограничения текущего шага

1. Это ещё не production-публикация `radar.aipractice.space`.
2. `systemd`, Nginx и TLS не менялись.
3. Frontend — рабочий скелет для проверки данных, а не финальная пиксельная реализация design handoff.
4. LLM-классификатор сейчас отработал fallback-правилами, потому что production-провайдер не подключён через `RADAR_LLM_*`.
5. Полный сетевой backfill дат запущен, но домены со статусом `unresolved` требуют повторной проверки или специальных правил извлечения.

## Следующий шаг

Следующий безопасный шаг — разобрать `unresolved` и `low_confidence`: выделить домены, которые блокируют доступ, добавить специальные правила для YouTube/PRNewswire/страниц без нормального OpenGraph, затем повторить только очередь проблемных ссылок.

## Продолжение реализации: качество дат и API-контракт

Добавлен второй слой после первичного backfill:

1. Создана миграция `/mnt/vdd/Radar/data/db/migrations/002_quality_and_search.sql`.
2. В `materials` добавлены поля `brief`, `theses_json`, `trend_notes` под расширение публичного API и будущую LLM-аналитику.
3. Созданы таблицы `source_domain_rules` и `material_date_quality`.
4. Добавлен FTS5-индекс `materials_fts` для поиска по заголовку, сути, выводу AgPM, источнику и URL.
5. Создан скрипт `/mnt/vdd/Radar/pipeline/scripts/agpm_radar_quality.py`.
6. Daily pipeline `/mnt/vdd/Radar/pipeline/bin/radar_daily_publish.sh` теперь запускает quality-слой перед экспортом JSON-кэша.
7. Backend API расширен эндпоинтами и полями:
   - `/api/issue/latest`;
   - `/api/search`;
   - `/api/internal/date-quality`;
   - `date_quality` в payload последнего выпуска и в карточках материалов.
8. JSON-кэш теперь содержит `date_quality` в `latest.json` и отдельный файл `/mnt/vdd/Radar/data/exports/json-cache/date-quality-summary.json`.
9. Внутренняя очередь проверки дат экспортируется в `/mnt/vdd/Radar/data/exports/json-cache/internal/date-quality.json`.
10. `agpm_radar_docx_backfill.py` теперь читает локальный metadata-кэш даже без сетевого режима, чтобы ежедневный backfill не затирал уже найденные `resolved`-даты публикации.

Результат диагностики на текущей базе:

- всего материалов: 121;
- уверенная дата публикации: 64;
- дата с низкой уверенностью: 49;
- дата не найдена: 8;
- очередь ручной проверки: 63 материала;
- мягкий monitoring без включения в очередь редактора: 28 материалов;
- без замечаний по дате: 30 материалов;
- высокие риски по датам: 10;
- средние риски по датам: 53;
- низкие риски по датам: 28.

Проверка:

- `python3 -m py_compile` для backend API, export и quality-скрипта прошёл;
- миграция применена через `python3 init_radar_db.py`;
- `python3 agpm_radar_quality.py` пересобрал очередь и FTS-индекс;
- `python3 agpm_radar_site_export.py` пересобрал JSON-кэш;
- временный API на порту `8766` вернул HTTP 200 для `/api/health`, `/api/issue/latest`, `/api/internal/date-quality?limit=2`, `/api/search?q=governance&limit=3`.
- полный `bash /mnt/vdd/Radar/pipeline/bin/radar_daily_publish.sh` прошёл без сетевого fetch и сохранил распределение дат: 64 `resolved`, 49 `low_confidence`, 8 `unresolved`; FTS-индекс содержит 121 материал.

Production-сервисы, Nginx, TLS и systemd по-прежнему не менялись.

## Продолжение реализации: deploy-пакет без включения production

Подготовлен deploy-пакет в `/mnt/vdd/Radar/deploy/`. Активные системные конфигурации не изменялись.

Read-only проверка окружения показала, что на сервере используется Caddy, а не Nginx:

- установлен `/usr/bin/caddy`;
- `caddy.service` включён;
- `nginx` не найден;
- основной конфиг: `/etc/caddy/Caddyfile`;
- существующие публичные каталоги в `/srv` в основном принадлежат пользователю `caddy`.

Созданы файлы:

- `/mnt/vdd/Radar/deploy/radar-api.service` — systemd unit для backend API;
- `/mnt/vdd/Radar/deploy/radar-daily-publish.service` — oneshot service для daily pipeline;
- `/mnt/vdd/Radar/deploy/radar-daily-publish.timer` — timer на 08:00 Europe/Moscow;
- `/mnt/vdd/Radar/deploy/Caddyfile.radar.aipractice.space` — основной Caddy-шаблон публикации;
- `/mnt/vdd/Radar/deploy/nginx-radar.aipractice.space.conf` — запасной Nginx-шаблон;
- `/mnt/vdd/Radar/deploy/production-checklist.md` — пошаговый план включения, проверки и rollback;
- `/mnt/vdd/Radar/pipeline/bin/radar_healthcheck.sh` — локальный healthcheck API/frontend/latest issue.

Проверка:

- `caddy fmt --overwrite /mnt/vdd/Radar/deploy/Caddyfile.radar.aipractice.space` выполнен;
- `caddy validate --config /mnt/vdd/Radar/deploy/Caddyfile.radar.aipractice.space` вернул `Valid configuration`;
- `systemd-analyze verify` для трёх unit/timer-файлов прошёл без ошибок;
- `bash -n` для `radar_healthcheck.sh` и `radar_daily_publish.sh` прошёл;
- `/mnt/vdd/Radar/pipeline/bin/radar_healthcheck.sh` на локальном API/frontend прошёл: latest issue `2026-07-29`, 13 материалов.

Production-сервисы, `/etc/caddy/Caddyfile`, systemd, DNS и публичный домен `radar.aipractice.space` не менялись.

## Production-включение

После отдельной команды пользователя выполнено production-включение `https://radar.aipractice.space/`.

Что изменено:

1. Сделаны резервные копии:
   - SQLite: `/mnt/vdd/Radar/data/db/backups/radar.sqlite.20260729T171959Z.bak`;
   - Caddyfile: `/etc/caddy/Caddyfile.backup-before-radar-20260729T171959Z`.
2. Остановлен ручной dev API на `127.0.0.1:8765`.
3. Установлены systemd-файлы:
   - `/etc/systemd/system/radar-api.service`;
   - `/etc/systemd/system/radar-daily-publish.service`;
   - `/etc/systemd/system/radar-daily-publish.timer`.
4. Выполнен `systemctl daemon-reload`.
5. Включён и запущен `radar-api.service`.
6. Включён и запущен `radar-daily-publish.timer`.
7. В `/etc/caddy/Caddyfile` добавлен блок `radar.aipractice.space`.
8. Выполнены `caddy fmt --overwrite /etc/caddy/Caddyfile` и `caddy validate --config /etc/caddy/Caddyfile`.
9. Выполнен `systemctl reload caddy`.
10. Caddy автоматически получил сертификат Let's Encrypt для `radar.aipractice.space`.
11. После публикации остановлен ручной frontend-сервер на `8780`; публичная раздача идёт через Caddy.

Дополнительная правка перед финальной проверкой:

- в `/mnt/vdd/Radar/work/radar-app/app.js` исправлен выбор API origin: локальная разработка на `127.0.0.1:8780` продолжает ходить в `127.0.0.1:8765`, а публичный сайт использует `window.location.origin`, то есть `/api` на `https://radar.aipractice.space`.

Проверки после включения:

- `radar-api.service`: active, enabled;
- `radar-daily-publish.timer`: active, enabled;
- `caddy.service`: active, enabled;
- timer показывает следующий запуск `2026-07-30 05:00:00 UTC` — это 08:00 МСК;
- `systemctl start radar-daily-publish.service` прошёл успешно, exit code 0;
- `https://radar.aipractice.space/` отдаёт HTTP 200;
- `https://radar.aipractice.space/api/issue/latest` отдаёт HTTP 200, выпуск `2026-07-29`, 13 материалов;
- `RADAR_API_URL=https://radar.aipractice.space RADAR_FRONT_URL=https://radar.aipractice.space /mnt/vdd/Radar/pipeline/bin/radar_healthcheck.sh` прошёл;
- Playwright smoke по публичному URL прошёл для desktop `day`, `7d`, `30d + Governance` и mobile `390px`: консоль без ошибок, ключевые секции заполнены, горизонтального overflow нет;
- после остановки dev frontend на `8780` публичный URL продолжил отдавать HTTP 200.

Rollback зафиксирован в `/mnt/vdd/Radar/deploy/production-checklist.md`.

## Продолжение реализации: frontend по handoff

Входящий архив handoff распакован в `/mnt/vdd/Radar/work/design_handoff_radar_agpm/`. Основной референс: `Радар AgPM.dc.html`; README, шрифты, favicon и эталонные скриншоты сохранены рядом.

Обновлён рабочий frontend `/mnt/vdd/Radar/work/radar-app/`:

1. Подключены локальные шрифты Golos Text и PT Mono из handoff.
2. Заменён favicon на вариант из handoff.
3. Пересобрана структура главной страницы:
   - sticky-шапка;
   - две строки фильтров;
   - сводка выпуска со sparkline;
   - блок «Что важно для AgPM сегодня»;
   - виджет радара с режимами «Сонар», «Доли периметров», «Кольцо 30 дней»;
   - три периметра материалов;
   - динамика трендов, heatmap, рубрики и источники;
   - хронология выпусков;
   - рубрикатор;
   - тёмный подвал.
4. Фронт подключён к живому API: `/api/issue/latest`, `/api/materials`, `/api/timeseries`, `/api/rubrics`, `/api/sources`, `/api/issues`, `/api/issue/{date}`.
5. Реализованы интеракции периода, периметра, verdict, рубрики, поиска, сброса фильтров и раскрытия выпуска в хронологии.

Проверка:

- `node --check /mnt/vdd/Radar/work/radar-app/app.js` прошёл;
- frontend `http://127.0.0.1:8780/` отдаёт HTTP 200;
- локальный dev API перезапущен на `127.0.0.1:8765`;
- проверены API-запросы для latest, фильтра `perimeter=mid`, FTS-поиска `q=governance`.

Ограничение: полноценная визуальная проверка через Playwright/Chromium в текущем окружении не выполнена, потому что Playwright и браузерный бинарь здесь не установлены.

## Продолжение реализации: визуальная QA-проверка

Для проверки frontend создан отдельный локальный QA-контур `/mnt/vdd/Radar/work/qa/` с Playwright. Chromium установлен в пользовательский cache Playwright; production-контур и frontend-каталог зависимостями не засорялись.

Добавлен smoke-тест `/mnt/vdd/Radar/work/qa/visual-smoke.js`. Он проверяет:

- загрузку `http://127.0.0.1:8780/`;
- консольные ошибки и `pageerror`;
- desktop-состояния: `day`, `7d`, `30d + Governance`;
- mobile-состояние `390px`;
- отсутствие горизонтального overflow;
- наличие ключевых секций: тренды, heatmap, хронология, рубрикатор, footer sources;
- базовые счётчики карточек и колонок.

Скриншоты сохранены в `/mnt/vdd/Radar/work/qa-screenshots/`:

- `desktop-day.png`;
- `desktop-7d.png`;
- `desktop-30d-governance.png`;
- `mobile-day.png`;
- `qa-results.json`.

Первый прогон выявил мобильный горизонтальный overflow: `scrollWidth = 423` при `innerWidth = 390`. Причины:

- верхняя панель пыталась удержать brand и правый блок в одной строке;
- график трендов сохранял desktop-минимумы столбиков.

Исправлено в `/mnt/vdd/Radar/work/radar-app/styles.css`:

- на мобильной ширине topbar складывается в колонку;
- `topbar__right` занимает 100%;
- дата выпуска может переноситься;
- trend bars на мобильной ширине переходят в `grid-template-columns: repeat(30, minmax(0, 1fr))`;
- панели получили `min-width: 0`.

Повторный Playwright-прогон чистый:

- desktop `day`: HTTP 200, 13 карточек, overflow отсутствует, консоль чистая;
- desktop `7d`: HTTP 200, 16 карточек, overflow отсутствует, консоль чистая;
- desktop `30d + Governance`: HTTP 200, активный фильтр `Governance`, 14 карточек, overflow отсутствует, консоль чистая;
- mobile `390px`: HTTP 200, 13 карточек, `scrollWidth = 390`, overflow отсутствует, консоль чистая.

Дополнительная проверка:

- `python3 -m py_compile` для backend и pipeline-скриптов прошёл;
- `node --check /mnt/vdd/Radar/work/radar-app/app.js` прошёл;
- `http://127.0.0.1:8765/api/health` отдаёт 200;
- `http://127.0.0.1:8780/` отдаёт 200.

Production-сервисы, Nginx, TLS и systemd по-прежнему не менялись.

## Production cache-fix после пользовательской проверки

После публикации пользователь прислал скриншот, где фронт показывал нулевые счётчики и браузер запрашивал доступ к локальным приложениям и службам. Это указало на старую клиентскую версию `app.js`, которая в браузерном кэше могла продолжать обращаться к `127.0.0.1:8765`.

Диагностика:

- публичный `https://radar.aipractice.space/app.js` уже содержал исправленный выбор API origin через `window.location.origin`;
- Caddy отдавал JS/CSS с `Cache-Control: public, max-age=604800`;
- API `https://radar.aipractice.space/api/issue/latest` отвечал 200 и возвращал выпуск `2026-07-29` с 13 материалами.

Исправлено:

- в `/mnt/vdd/Radar/work/radar-app/index.html` добавлены version query для `favicon.svg`, `styles.css`, `app.js`: `v=20260729-1730`;
- в `/etc/caddy/Caddyfile` и `/mnt/vdd/Radar/deploy/Caddyfile.radar.aipractice.space` для HTML задан `Cache-Control: no-store`;
- для JS/CSS задан `Cache-Control: no-cache, max-age=0, must-revalidate`;
- для шрифтов и изображений сохранён недельный кэш;
- добавлен `Permissions-Policy` с запретом `local-network-access=()`;
- Caddyfile перед правкой сохранён в `/etc/caddy/Caddyfile.backup-before-radar-cachefix-20260729T172832Z`;
- Caddy прошёл `validate` и был перезагружен через `systemctl reload caddy`.

Проверка после исправления:

- `https://radar.aipractice.space/` отдаёт `Cache-Control: no-store`;
- `https://radar.aipractice.space/app.js?v=20260729-1730` отдаёт `Cache-Control: no-cache, max-age=0, must-revalidate`;
- HTML содержит `app.js?v=20260729-1730` и `styles.css?v=20260729-1730`;
- public healthcheck прошёл: latest issue `2026-07-29`, 13 материалов;
- Playwright smoke по публичному URL прошёл на desktop и mobile без console errors, request failures и overflow;
- отдельный request-аудит показал, что страница делает запросы только к `https://radar.aipractice.space/api/*`; обращений к `127.0.0.1`, `localhost` или `8765` из публичной страницы нет;
- контрольные показатели на странице: просмотрено `64`, включено `13`, периметры `2 / 11 / 0`.

## Замена favicon по пользовательскому SVG

Пользователь прислал новый SVG для favicon.

Сделано:

- старый файл сохранён как `/mnt/vdd/Radar/work/radar-app/favicon.svg.bak.before-user-favicon-20260729T173441Z`;
- `/mnt/vdd/Radar/work/radar-app/favicon.svg` заменён на присланный SVG;
- в `/mnt/vdd/Radar/work/radar-app/index.html` обновлён cache-busting query favicon: `v=20260729-1734`.

Проверка:

- XML-разбор `favicon.svg` прошёл;
- SHA-256 нового `favicon.svg` совпадает с присланным файлом;
- `https://radar.aipractice.space/favicon.svg?v=20260729-1734` отдаёт HTTP 200 и новый SVG;
- HTML публичной страницы содержит `./favicon.svg?v=20260729-1734`;
- public healthcheck прошёл;
- Playwright-проверка подтвердила новый favicon href, отсутствие console errors и рабочие счётчики: просмотрено `64`, включено `13`, карточек `13`.

## Разведение даты выпуска и даты публикации первоисточника

После пользовательской проверки логики отображения периода `День` выявлена неоднозначность:

- стартовый режим `День` показывал `latest.materials`, то есть состав последнего выпуска радара;
- пользователь ожидал в режиме `День` календарные материалы с реальной датой публикации первоисточника;
- графики и `timeseries` строились по `daily_stats`, где `stat_date` равен дате выпуска радара, а не дате публикации первоисточника;
- в карточке рядом с каналом обнаружения выводилась дата без пояснения, из-за чего `Perplexity fresh web research: middle 2026-07-01 ◆ Core` читалось как дата находки.

На примере FifthRow `AI Agent Orchestration Goes Enterprise` проверено:

- на странице первоисточника указано `4 May, 2026`;
- в базе ошибочно стояло `published_at = 2026-07-01`, `publication_date_status = low_confidence`;
- `first_seen_at = 2026-06-09T05:00:16+00:00`;
- `radar_issue_date = 2026-06-09`.

Исправлено:

- во фронте добавлен отдельный режим `Выпуск`, он стал стартовым и показывает состав последнего выпуска;
- режимы `День`, `7 дней`, `30 дней` работают как календарные периоды по `published_at`;
- в карточках вместо канала обнаружения как главного источника выводится домен первоисточника и явная метка `опубл.`;
- если дата публикации не найдена, карточка больше не подменяет её датой выпуска как будто это дата публикации;
- в истории изменена команда `состав дня` на `состав выпуска`;
- `/api/materials?period=day` теперь фильтрует `date(published_at) = today`;
- `/api/materials?period=7d|30d` фильтрует только материалы с найденной `published_at`;
- `/api/timeseries` теперь строит календарный ряд по `published_at`, а не по `daily_stats`;
- `/api/rubrics?period=30d` и `/api/sources?period=30d` считают агрегаты за 30 дней по `published_at`, а не по всей базе;
- `/api/issue/latest` дополнен `issue_stats`, чтобы статистика выпуска была отделена от календарной статистики публикаций;
- в `agpm_radar_docx_backfill.py` добавлена поддержка англоязычного формата даты `4 May, 2026` и раннего текста статьи.
- исправлен поиск: FTS5-ранжирование теперь использует `bm25(materials_fts)`, fallback ищет также по `url` и `canonical_url`.
- в `index.html` обновлена версия фронтового скрипта до `app.js?v=20260729-1808`.

Данные FifthRow исправлены:

- `published_at = 2026-05-04`;
- `publication_date_source = article_lead_text`;
- `publication_date_confidence = 0.72`;
- `publication_date_status = resolved`.

Проверка:

- `python3 -m py_compile` для API и pipeline-скриптов прошёл;
- `node --check app.js` прошёл;
- `radar-api.service` перезапущен, статус `active/enabled`;
- `https://radar.aipractice.space/api/issue/latest` возвращает 13 материалов последнего выпуска;
- `https://radar.aipractice.space/api/materials?period=day` возвращает 0 материалов на 2026-07-29, потому что публикаций первоисточников с этой датой нет;
- `https://radar.aipractice.space/api/materials?period=7d` возвращает 14 материалов по реальным датам публикации;
- `https://radar.aipractice.space/api/timeseries?days=30` возвращает 30 календарных дней по `published_at`;
- `https://radar.aipractice.space/api/rubrics?period=30d` и `/api/sources?period=30d` возвращают агрегаты по 30-дневному окну публикаций;
- `https://radar.aipractice.space/api/search?q=fifthrow` возвращает FifthRow-запись с `published_at = 2026-05-04`;
- public healthcheck прошёл;
- Playwright smoke по публичному URL прошёл для `desktop-issue`, `desktop-day`, `desktop-7d`, `desktop-30d-governance`, `mobile-day` без console errors и horizontal overflow.

## Подключение блока «Что важно для AgPM сегодня» к данным выпуска

После проверки выяснилось, что блок `Что важно для AgPM сегодня` на публичном сайте брал `issue.theses` из API, но для текущего выпуска `issues.theses_json` был пустым. Поэтому фронт показывал fallback-тезисы, зашитые в `app.js`, а не аналитику конкретного выпуска.

Исправлено:

- добавлен скрипт `/mnt/vdd/Radar/pipeline/scripts/agpm_radar_issue_theses.py`;
- скрипт строит 3–4 тезиса выпуска по реальным включённым материалам из SQLite: периметры, рубрики, flags `governance/security/human_in_the_loop/pmo/isup/mcp`, ключевые слова в `summary` и `agpm_takeaway`;
- скрипт записывает результат в `issues.theses_json`, а краткое описание выпуска — в `issues.brief`;
- daily pipeline `/mnt/vdd/Radar/pipeline/bin/radar_daily_publish.sh` теперь запускает `agpm_radar_issue_theses.py` после `agpm_radar_llm_classify.py` и перед `agpm_radar_quality.py`;
- тезисы пересчитаны для всех 53 выпусков;
- JSON-cache пересобран.

Для выпуска `2026-07-29` сформированы 4 тезиса:

- `Главный сигнал выпуска — управляемая агентность.`
- `Рубрики выпуска показывают смещение к управленческой инфраструктуре.`
- `Ответственность человека остаётся ограничителем автономии.`
- `Близкий периметр держится на прикладных PMO-сценариях.`

Проверка:

- `python3 -m py_compile` для нового скрипта и связанных backend/export-скриптов прошёл;
- `bash -n` для daily pipeline прошёл;
- `https://radar.aipractice.space/api/issue/latest` отдаёт `issue.theses` из 4 элементов и заполненный `issue.brief`;
- Playwright DOM-проверка публичной страницы подтвердила, что блок `Что важно для AgPM сегодня` показывает новые тезисы;
- public healthcheck прошёл;
- Playwright smoke по публичному URL прошёл без console errors, пустых ключевых секций и horizontal overflow.

## Замена периода `День` на `Вчера`

По уточнению пользователя переключатель верхней панели должен показывать не текущий день, а вчерашние новости по реальной дате публикации первоисточника.

Исправлено:

- во фронте `/mnt/vdd/Radar/work/radar-app/index.html` кнопка `День` заменена на `Вчера`;
- для кнопки задан период `data-period="yesterday"`;
- cache-busting для фронтового скрипта обновлён до `app.js?v=20260729-1828`;
- в `/mnt/vdd/Radar/backend/radar-api/server.py` добавлен период `yesterday`, который фильтрует материалы как `date(published_at) = date.today() - 1 day`;
- старый период `day` оставлен как совместимый режим «сегодня», чтобы не ломать возможные прямые API-вызовы;
- в `/mnt/vdd/Radar/pipeline/scripts/agpm_radar_site_export.py` добавлена поддержка `stats.yesterday` и периода `yesterday` для JSON-cache;
- QA-сценарий `/mnt/vdd/Radar/work/qa/visual-smoke.js` обновлён: проверки `desktop-day` и `mobile-day` заменены на `desktop-yesterday` и `mobile-yesterday`;
- `radar-api.service` перезапущен.

Проверка:

- `python3 -m py_compile /mnt/vdd/Radar/backend/radar-api/server.py /mnt/vdd/Radar/pipeline/scripts/agpm_radar_site_export.py` прошёл;
- `node --check /mnt/vdd/Radar/work/radar-app/app.js` прошёл;
- JSON-cache пересобран через `agpm_radar_site_export.py`;
- на 2026-07-29 локальный и публичный `/api/materials?period=yesterday` возвращают 11 материалов только с `published_at = 2026-07-28`;
- публичный `/api/issue/latest` отдаёт `stats.yesterday`: `viewed=11`, `included=11`, `near=0`, `mid=11`, `far=0`, `core=11`, `adjacent=0`;
- публичный HTML содержит `data-period="yesterday"`, текст `Вчера` и `app.js?v=20260729-1828`;
- public healthcheck прошёл с `RADAR_API_URL=https://radar.aipractice.space` и `RADAR_FRONT_URL=https://radar.aipractice.space`;
- Playwright smoke по публичному URL прошёл для `desktop-issue`, `desktop-yesterday`, `desktop-7d`, `desktop-30d-governance`, `mobile-yesterday` без ошибок консоли, пустых ключевых секций и horizontal overflow.

## Единый ежедневный конвейер 08:00 МСК

По уточнению пользователя ежедневный контур должен быть последовательным: сначала сбор ежедневного радара, затем формирование DOCX, пополнение wiki и отправка DOCX в Telegram, и только после этого перенос найденных и классифицированных материалов на публичный сайт.

Исправлено:

- `scripts/agpm_radar_daily.sh` в workspace теперь после сбора, DOCX и wiki синхронизирует `knowledge/agpm-radar/` в `/mnt/vdd/Radar/data/corpus/knowledge-agpm-radar/`;
- DOCX из `knowledge/agpm-radar/reports/` копируются в `/mnt/vdd/Radar/data/corpus/raw-docx/`, чтобы сайтовый backfill видел свежий выпуск;
- после успешной отправки DOCX в Telegram OpenClaw-задача запускает `/mnt/vdd/Radar/pipeline/bin/radar_daily_publish.sh`, который выполняет `init_radar_db.py`, `agpm_radar_docx_backfill.py`, `agpm_radar_llm_classify.py`, `agpm_radar_issue_theses.py`, `agpm_radar_quality.py` и `agpm_radar_site_export.py`;
- синхронная копия `/mnt/vdd/Radar/pipeline/scripts/agpm_radar_daily.sh` обновлена тем же порядком;
- OpenClaw cron `agpm_weekly_radar_daily_collect` обновлён: он проверяет свежий DOCX, run-log, state, синхронизацию корпуса на `/mnt/vdd/Radar` и `latest.json`;
- `radar-daily-publish.timer` отключён, чтобы не было второго параллельного запуска в 08:00 МСК.

Проверка:

- `bash -n` для обоих `agpm_radar_daily.sh` прошёл;
- `bash -n /mnt/vdd/Radar/pipeline/bin/radar_daily_publish.sh` прошёл;
- OpenClaw cron показывает следующий запуск `2026-07-30 08:00 MSK`;
- `radar-daily-publish.timer` находится в состоянии `inactive/disabled`.
