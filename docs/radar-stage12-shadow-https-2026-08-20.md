# Radar V2 Stage 12 — публичный shadow hostname и HTTPS

Дата приёмки: 2026-08-20.

## Итог

Stage 12 завершён. Radar V2 доступен как shadow-система по адресу
`https://radar.agpm.space` на Local Ru `147.45.99.225`. Основной Legacy-домен
`radar.aipractice.space` и Legacy production не переключались.

## DNS и TLS

- authoritative DNS: `ns1.reg.ru`, `ns2.reg.ru`;
- `radar.agpm.space A 147.45.99.225` подтверждён на обоих authoritative NS;
- Cloudflare, Google и Quad9 возвращают тот же IPv4;
- Caddy получил сертификат Let's Encrypt для единственного SAN `radar.agpm.space`;
- срок сертификата: 2026-08-20 — 2026-11-18;
- HTTP отвечает `308` на `https://radar.agpm.space/`.

## Caddy boundary

Отдельный vhost:

- проксирует `/api/*` и `/gazettes/*` только на `127.0.0.1:8765`;
- раздаёт immutable `/assets/styles.css` и `/assets/app.mjs` из application release;
- разрешает SPA-маршруты `/`, `/issues`, `/issues/*`, `/search`, `/gazettes`;
- для всех остальных маршрутов отвечает `404`;
- удаляет backend `Server` header на reverse-proxy ответах;
- задаёт CSP, CORP, Permissions-Policy, Referrer-Policy, nosniff и DENY framing;
- не меняет vhost `nrd.aipractice.space`.

Перед заменой Caddyfile создана отдельная backup-копия. Кандидат и установленный файл успешно
прошли `caddy validate`; применён graceful `systemctl reload caddy`.

## Публичная приёмка

- `/`, `/issues`, `/issues/2026-08-20`, `/search`, `/gazettes`: `200` SPA;
- `/assets/styles.css`, `/assets/app.mjs`: `200`;
- `/api/health`, `/api/issues`, `/api/search?q=test`: `200`;
- `/admin`, `/api/private`, `/does-not-exist`: `404`;
- API health вернул release `rel_e404ff802c3e3c71083529ed` и logical state
  `ef5b4c3ef7ddfcda05c5aad331043bcc576ec641683e05d74ce1162e1e7c7f41`;
- backend `Server` header на `/api/health` отсутствует;
- `radar-v2-api` продолжает слушать только `127.0.0.1:8765`.

В full seed Stage 11 последний импортированный выпуск — `2026-08-19`; поэтому публичный API
деталей выпуска `2026-08-20` ожидаемо отвечает `404`. Импорт/доставка нового Legacy-выпуска
относится к будущему publisher transport stage, а не к Stage 12.

## Непрерывность сервисов

- `caddy.service`: PID `1021`, `NRestarts=0`;
- `radar-v2-api.service`: PID `23919`, `NRestarts=0`;
- все семь фактических NRD units active;
- публичный NRD health: API и worker `ok`;
- NRD, Radar API и Hermes listeners остались в прежнем loopback/external boundary.

## Не входило в Stage 12

- publisher transport и Project Manager delivery;
- cron/timer для Radar V2;
- reboot rehearsal;
- переключение основного `radar.aipractice.space`;
- удаление Legacy, backup или migration artifacts.

Следующий последовательный этап — Stage 13 по утверждённому плану с отдельной проверкой его
точной authority boundary перед изменениями.
