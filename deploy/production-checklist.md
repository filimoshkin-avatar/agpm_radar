# Production-чеклист Radar

Дата подготовки: 2026-07-29

Изначально этот пакет был подготовлен без применения к системе. Production-включение выполнено 2026-07-29 после отдельной команды пользователя.

## Статус применения

Production включён 2026-07-29:

- `radar-api.service` установлен в `/etc/systemd/system/`, включён и активен;
- `radar-daily-publish.service` установлен в `/etc/systemd/system/`;
- `radar-daily-publish.timer` установлен, но отключён 2026-07-29, чтобы не конкурировать с OpenClaw-конвейером на 08:00 МСК;
- ежедневный запуск выполняет OpenClaw cron `agpm_weekly_radar_daily_collect`: сбор источников, DOCX, wiki, синхронизация корпуса на `/mnt/vdd/Radar`, отправка DOCX в Telegram, затем классификация и экспорт сайта идут последовательно;
- блок `radar.aipractice.space` добавлен в `/etc/caddy/Caddyfile`;
- Caddy перезагружен без остановки сервиса;
- сертификат Let's Encrypt для `radar.aipractice.space` получен автоматически;
- ручной dev API на `127.0.0.1:8765` заменён systemd-сервисом;
- ручной frontend-сервер на `8780` остановлен после публикации.

Резервные копии перед включением:

- база: `/mnt/vdd/Radar/data/db/backups/radar.sqlite.20260729T171959Z.bak`;
- Caddyfile: `/etc/caddy/Caddyfile.backup-before-radar-20260729T171959Z`.

Дополнительная правка после пользовательской проверки:

- пользовательский браузер показал пустой фронт и запрос доступа к локальным службам;
- причина: старая клиентская версия `app.js` могла оставаться в недельном browser cache и обращаться к `127.0.0.1:8765`;
- в `index.html` добавлен version query для `favicon.svg`, `styles.css` и `app.js`;
- для HTML выставлен `Cache-Control: no-store`;
- для JS/CSS выставлен `Cache-Control: no-cache, max-age=0, must-revalidate`;
- для шрифтов и изображений сохранён недельный кэш;
- добавлен `Permissions-Policy` с запретом `local-network-access=()`;
- Caddyfile перед правкой сохранён в `/etc/caddy/Caddyfile.backup-before-radar-cachefix-20260729T172832Z`.

## Что уже готово

- Backend API: `/mnt/vdd/Radar/backend/radar-api/server.py`.
- Frontend: `/mnt/vdd/Radar/work/radar-app/`.
- SQLite: `/mnt/vdd/Radar/data/db/radar.sqlite`.
- Daily pipeline: `/mnt/vdd/Radar/pipeline/bin/radar_daily_publish.sh`.
- Healthcheck: `/mnt/vdd/Radar/pipeline/bin/radar_healthcheck.sh`.
- Systemd-шаблоны: `/mnt/vdd/Radar/deploy/radar-api.service`, `/mnt/vdd/Radar/deploy/radar-daily-publish.service`, `/mnt/vdd/Radar/deploy/radar-daily-publish.timer`.
- Caddy-шаблон: `/mnt/vdd/Radar/deploy/Caddyfile.radar.aipractice.space`.
- Nginx-шаблон: `/mnt/vdd/Radar/deploy/nginx-radar.aipractice.space.conf` — запасной вариант, если позднее будет принято решение перейти на Nginx.

## Фактическое окружение

Read-only проверка 2026-07-29 показала:

- установлен `/usr/bin/caddy`;
- `caddy.service` включён;
- `nginx` в системе не обнаружен;
- основная конфигурация: `/etc/caddy/Caddyfile`;
- существующие публичные сайты в `/srv` в основном принадлежат пользователю `caddy`.

Поэтому основной план публикации ниже ориентирован на Caddy, а не на Nginx.

## План включения production

1. Сделать резервную копию базы:
   `cp /mnt/vdd/Radar/data/db/radar.sqlite /mnt/vdd/Radar/data/db/backups/radar.sqlite.$(date -u +%Y%m%dT%H%M%SZ).bak`

2. Остановить локальный dev API на порту `8765`, если он запущен вручную.

3. Установить systemd unit:
   `cp /mnt/vdd/Radar/deploy/radar-api.service /etc/systemd/system/radar-api.service`

4. Установить daily service/timer:
   `cp /mnt/vdd/Radar/deploy/radar-daily-publish.service /etc/systemd/system/radar-daily-publish.service`
   `cp /mnt/vdd/Radar/deploy/radar-daily-publish.timer /etc/systemd/system/radar-daily-publish.timer`

5. Перечитать systemd:
   `systemctl daemon-reload`

6. Включить API:
   `systemctl enable --now radar-api.service`

7. Проверить локальный API, если нужен dev-контур:
   `/mnt/vdd/Radar/pipeline/bin/radar_healthcheck.sh --local`

8. Сделать резервную копию Caddyfile:
   `cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.backup-before-radar-$(date -u +%Y%m%dT%H%M%SZ)`

9. Добавить блок из `/mnt/vdd/Radar/deploy/Caddyfile.radar.aipractice.space` в `/etc/caddy/Caddyfile`.

10. Проверить Caddy:
    `caddy validate --config /etc/caddy/Caddyfile`

11. Перезагрузить Caddy:
    `systemctl reload caddy`

12. Ежедневный timer не включать, если активен OpenClaw cron `agpm_weekly_radar_daily_collect`.
    При ручном emergency-переносе публикации на systemd сначала отключить OpenClaw cron, затем включать `radar-daily-publish.timer`, чтобы не получить параллельный запуск.

13. Финальная production-проверка:
    `/mnt/vdd/Radar/pipeline/bin/radar_healthcheck.sh`

## Rollback

1. Отключить timer:
   `systemctl disable --now radar-daily-publish.timer`

2. Отключить API:
   `systemctl disable --now radar-api.service`

3. Вернуть предыдущий Caddyfile из резервной копии:
   `cp /etc/caddy/Caddyfile.backup-before-radar-20260729T171959Z /etc/caddy/Caddyfile`
   `caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy`

4. Вернуть резервную копию базы, если проблема связана с миграцией или pipeline:
   `cp /mnt/vdd/Radar/data/db/backups/radar.sqlite.20260729T171959Z.bak /mnt/vdd/Radar/data/db/radar.sqlite`

## Замечания перед включением

- Сейчас на `127.0.0.1:8765` может быть запущен ручной dev API; перед установкой systemd-сервиса его нужно остановить.
- В текущей системе по списку `/srv` видны сайты под пользователем `caddy`, но для Radar целевой root остаётся `/mnt/vdd/Radar/work/radar-app`, чтобы не уводить рабочие данные с нового диска.
- Набор источников не управляется с фронта; изменения источников должны идти через backend/pipeline.
- Полный список отсечённых материалов не должен отдаваться публичным API.
