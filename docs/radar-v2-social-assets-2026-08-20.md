# Radar V2 — Legacy favicon и social preview assets

Дата: 2026-08-20.
Статус: принят и опубликован на `https://radar.agpm.space/`.

## Изменение

Radar V2 получил те же канонические файлы, которые отдаёт Legacy production:

- `/favicon.svg` — SHA-256
  `4757342b86258c1fd7f9e08c4bc66b5e6af3014d5c6ab4b8ca1a4914524e7b38`;
- `/og-image-20260803.png` — PNG `1200x630`, SHA-256
  `1805d2711f4f7a4dd6118afc9900a314472383ace8ad9c0c98c26281f0c2b430`.

Главная страница содержит Legacy-equivalent description, Open Graph и Twitter Card metadata, но
канонические `og:url`, `og:image` и `twitter:image` указывают на `https://radar.agpm.space`.

## Regression boundary

- точные SHA обоих assets закреплены тестами;
- оба файла обязаны входить в immutable web role artifact;
- isolation scanner допускает только pinned PNG с точным SHA и только два канонических social URL;
- неизвестные бинарные файлы и любые другие remote web URLs продолжают fail closed;
- Caddy разрешает только `/favicon.svg` и `/og-image-20260803.png` как отдельные public assets.

## Проверки

Канонический `v2/scripts/verify.sh`:

- Ruff format/lint: PASS;
- strict mypy: 72 source files PASS;
- pytest: 145 passed;
- contracts: 6 schemas, 8 examples, 23 SQLite tables, 11 public API paths PASS;
- JavaScript syntax и frontend console smoke: PASS;
- secret/Legacy-isolation scan: 90 files, 3 fixtures PASS;
- deterministic production artifact: PASS.

## Immutable release и активация

```text
Git commit: 21b111ae2e934ef7c6b14718868bad464f19c6e9
Application release: app_release_20260820_21b111a
Outer package SHA-256: 4259be103104bda3d561fc14ce6db8749e884a9ee1386ae26dfcab716cd1d038
API role SHA-256: c807e9208aa811a0bb47b3341ebf4a4f4f4ff7dd628f7911cc62e03d6680c0e3
Web role SHA-256: f6bb420221f748340260ca39610e0dbccdb05c0c4d29ec9fa98c96db232060a1
```

API role hash совпал с предыдущим production release. Новые create-only API/web directories
установлены и обе `current` ссылки атомарно переведены на один release ID. Старый
`app_release_20260819_545bf2e` сохранён как rollback target. Radar API не перезапускался.

Caddyfile предварительно сохранён в отдельную backup-копию, кандидат и установленный config
прошли `caddy validate`, затем применён graceful reload.

## Live acceptance

- оба asset endpoint возвращают `200`, правильный MIME и `Cache-Control: public, max-age=86400`;
- скачанные через public HTTPS файлы совпадают с Legacy по SHA-256;
- live HTML содержит полный OG/Twitter contract и новый canonical hostname;
- Radar API health сохранил release данных `rel_e404ff802c3e3c71083529ed` и прежний state hash;
- Caddy PID `1021`, Radar API PID `23919`, у обоих `NRestarts=0`;
- все семь NRD services active, публичный NRD health green;
- error-priority Caddy journal после reload пуст.

Legacy production, publisher, cron, DNS и content database не изменялись. Старые releases,
packages, worktree и backup evidence сохранены; ничего не удалялось.
