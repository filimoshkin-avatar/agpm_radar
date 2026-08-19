# AgPM Radar

AgPM Radar — публичное приложение ежедневного радара агентного проектного управления.

Репозиторий фиксирует код приложения, миграции, шаблоны deploy и правила выпуска. Операционные данные, SQLite-базы, кэши, логи, raw-корпус, резервные копии и секреты остаются вне git.

## Контуры

- Legacy development/production: этот сервер, рабочий каталог `/mnt/vdd/Radar`; текущий `radar.aipractice.space` продолжает работать без архитектурных изменений до отдельного cutover-решения.
- Radar V2 development: параллельная пересборка на этом сервере с отдельными кодом, SQLite, очередями и публикационным состоянием.
- Radar V2 shadow production: Local Ru `147.45.99.225` под отдельным hostname для длительного сравнения с Legacy до явного принятия владельцем.
- Project Manager/OpenClaw: утренний cron, сбор, редакторский отбор, ручные корректировки, LLM/fallback, Telegram и формирование candidate packages.
- Radar V2 publisher: детерминированные проверки, DOCX/JSON, SQLite delta, доставка, атомарная активация, healthcheck и rollback.

## Основные каталоги

- `v2/` — изолированный Radar V2 application workspace, locked gates и production artifact builder.
- `backend/radar-api/` — Legacy публичный JSON API.
- `pipeline/` — Legacy миграции, экспорт, quality gates и публикационный pipeline.
- `work/radar-app/` — Legacy frontend.
- `deploy/` — Legacy systemd, Caddy/Nginx и production-чеклисты.
- `docs/` — архитектурные решения и регламенты.

## Правило разработки

Legacy остаётся рабочим эталонным контуром и не перестраивается на месте. Radar V2 создаётся параллельно. Любой coding-агент может готовить изменения через git, но application release на Local Ru выполняется только из проверенного commit/tag после явного подтверждения владельца. Content и gazette releases проходят только через детерминированный publisher; production вручную не редактируется.

Release gates зависят от типа релиза:

- **Application:** clean commit/tag, полный test/build/security gate, явное подтверждение владельца, backup, versioned migrations только при изменении схемы, атомарная активация, controlled service reload и rollback rehearsal.
- **Content:** проверенный candidate без SQL/DDL/migrations, source/production staging copies, row-level delta, integrity/FK/per-table hashes, атомарный content pointer, обязательное переоткрытие SQLite API, проверка ожидаемого release id/hash и audit result.
- **Gazette:** проверенный immutable HTML/assets package, security/link/visual/print gates, атомарная активация и public smoke.

Schema migrations никогда не запускаются обычной daily/correction/gazette публикацией. Любая ошибка после content activation обязана вернуть предыдущий pointer, переоткрыть API и подтвердить прежний release id/hash; иначе следующие публикации блокируются до reconciliation.

Историческая модель первого выделения репозитория сохранена в `docs/development-production-model-2026-08-19.md`.

Согласованный master plan полной пересборки Radar V2, dual-run и миграции на Local Ru: `docs/migration-plan-review-2026-08-19.md`.

Read-only снимок Legacy перед началом реализации V2: `docs/legacy-baseline-2026-08-19.md`; обезличенные regression fixtures находятся в `fixtures/legacy-baseline/`.

Stage 0A containment публичных Legacy backup/temp/source-map artifacts: `docs/radar-stage0a-caddy-containment-2026-08-19.md`.

Stage 1 accepted ADRs and machine-readable contract family: `docs/radar-stage1-contracts-2026-08-19.md`, `docs/adr/`, `contracts/v1/`.

Stage 2 isolated Python/web skeleton, locked local/CI gates and deterministic production artifact:
`docs/radar-stage2-skeleton-2026-08-19.md`, `docs/adr/0003-radar-v2-python-stack-and-isolated-skeleton.md`, `v2/`.
