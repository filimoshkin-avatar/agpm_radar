# Legacy AgPM Radar baseline

Дата снимка: 2026-08-19

Статус: Stage 0 evidence; runtime не изменялся

## 1. Назначение

Этот документ фиксирует работающий Legacy Radar до начала реализации Radar V2. Он нужен для:

- доказательства, что Legacy не был изменён в ходе Stage 0;
- воспроизведения исторического импорта;
- сравнения Legacy и V2 на одинаковых входных данных;
- фиксации известных fallback и дефектов, которые V2 не должен переносить случайно;
- определения проверяемых acceptance gates следующих этапов.

Stage 0 создаёт только документацию, exporter и обезличенные fixtures в git. Cron, SQLite, Caddy, systemd, frontend, Project Manager и Local Ru не изменяются.

## 2. Git baseline

- Repository: `/mnt/vdd/Radar`.
- Branch: `main`.
- Baseline commit: `55a47f7301dda295c2b22e1625a30eab3897161b` (`Rewrite Radar V2 migration master plan`).
- Baseline tree: `706cf9bad967cff3fbb1e9c5153ca34040b739f4`.
- Git remote: отсутствует.
- Git tags: отсутствуют.
- До Stage 0 рабочее дерево было чистым.
- Изменения Stage 0 классифицированы отдельно: baseline report, sanitized fixtures и read-only fixture exporter.

Критичные Legacy-файлы и SHA-256:

| Файл | SHA-256 |
|---|---|
| `backend/radar-api/server.py` | `ddd03bb84da0b00b8d0a229d565cf4229f9ca84b7e0589b81efcea4075815050` |
| `pipeline/bin/radar_daily_publish.sh` | `bbb80ca46d2ee2074c3ab6dc5660f5e95c6f394d863ec699bcd94f23e9aa7a90` |
| `pipeline/bin/radar_healthcheck.sh` | `1dda58e1c9e8910dc457b140bf7fc1cc2b0c973875e19a896770ba7ca0f6a4cd` |
| `pipeline/scripts/agpm_radar_daily.sh` | `e5cdffaf7aa8ffb9f47aec324d066a5b9fc56929020c4155ecd1c4520d8d44fc` |
| `pipeline/scripts/agpm_radar_report.py` | `a4d97a0bf543a40a0bb38cbeca354e82aa2eca3a1fdf56f71ecc9d3b10b50175` |
| `pipeline/scripts/agpm_radar_docx_backfill.py` | `70a0767855226e494d39d6ee7a53bf1890490259475ec82a8bf52e5b30492c07` |
| `pipeline/scripts/agpm_radar_openclaw_analysis.py` | `b452db5ec0e0fef4d539795daf984e5eb594c0908269937071b0b68e17132c09` |
| `work/radar-app/index.html` | `5bc8d09765e2957361c6ab2687f10081dc40976a624ccd9b8f4755f1137d29cf` |
| `work/radar-app/app.js` | `31654e87ce46e5f6d211c8f4a3a7c3898bfab229f51d3d787392363e2b85c087` |
| `work/radar-app/styles.css` | `4e322a1a4c0b68b0cc8e7f18cc4468f705e603a59753d4d73a34fdff03ccffe4` |

## 3. Runtime versions and storage

- OS runtime Python: `3.12.3`.
- Python SQLite runtime: `3.45.1`.
- SQLite compile options include `ENABLE_FTS5`.
- Node.js: `22.23.2`.
- Caddy: `2.6.2`.
- OpenClaw CLI: `2026.7.1-2 (0790d9f)`.
- Radar filesystem: ext4 `/dev/sdb`, 20 GiB total, 13 GiB used, 6.4 GiB available at inventory time.

Approximate Legacy directories:

- `backend`: 204 KiB;
- `pipeline`: 1.7 MiB;
- `work`: 48 MiB;
- `data`: 587 MiB;
- `backups`: 410 MiB.

## 4. Project Manager cron ownership

Cron ownership доказан через live CLI Project Manager instance:

```text
OPENCLAW_CONFIG_PATH=/root/.openclaw-projectmanager/openclaw.json
OPENCLAW_STATE_DIR=/root/.openclaw-projectmanager
OPENCLAW_GATEWAY_PORT=18795
openclaw cron list --json
```

Фактическое состояние:

- отдельный OpenClaw instance/state: `/root/.openclaw-projectmanager`;
- systemd unit: `openclaw-projectmanager-gateway.service`;
- unit enabled и active;
- gateway bind: loopback;
- effective gateway port: `18795`; base unit still names `18793`, but a systemd drop-in overrides it;
- active cron store: `/root/.openclaw-projectmanager/state/openclaw.sqlite`; migrated JSON cron files are historical, not the current source of truth;
- cron owner: `agentId=main` внутри Project Manager instance, не текущий main OpenClaw instance;
- job id: `cfa00d0b-ae82-4e88-9119-6d1844a6a728`;
- job name: `agpm_weekly_radar_daily_collect`;
- enabled: `true`;
- schedule: `0 8 * * *`, timezone `Europe/Moscow`;
- session target: isolated;
- timeout: 3600 seconds;
- delivery: Telegram direct chat владельца;
- last run: `2026-08-19T05:00:00.010Z`;
- last status: `ok`;
- last duration: 700,450 ms;
- last delivery: delivered;
- next scheduled run at inventory: `2026-08-20T05:00:00Z`.

История job на момент снимка: 77 завершённых запусков с 2026-06-08, из них 72 `ok` и 5 `error`.

Run evidence:

- session file: `/root/.openclaw-projectmanager/agents/main/sessions/9688cc25-2679-43fd-bf5f-f6effb794c75.jsonl`;
- cron message received: `2026-08-19T05:00:01.613Z`;
- финальные действия зафиксированы около `05:11Z`;
- run выполнил сбор, отправку DOCX, site publish и сообщил об использовании `minimax/MiniMax-M3` после отказа основной OpenAI-модели.

Последовательность cron имеет важный внешний gate: сначала проверяется и отправляется DOCX; только после успешной Telegram-доставки вызывается site publisher. После публикации cron отдельно проверяет JSON-cache, SQLite LLM-строки и public API, затем отправляет финальный статус владельцу.

Legacy systemd timer `radar-daily-publish.timer` disabled/inactive и не является owner расписания.

## 5. Legacy daily sequence

### 5.1. Collection/editorial layer

Tracked compatibility entrypoint: `pipeline/scripts/agpm_radar_daily.sh`.

Рабочая копия в Project Manager workspace совпадает byte-for-byte с tracked-версией для:

- `agpm_radar_collect.py`;
- `agpm_radar_daily.sh`;
- `agpm_radar_report.py`;
- `agpm_radar_wiki.py`.

Последовательность:

1. загрузить `config/agpm-radar.env` из Project Manager workspace;
2. `agpm_radar_collect.py --run-id <date>`;
3. сформировать daily Markdown/DOCX;
4. если `included <= RADAR_LOW_YIELD_THRESHOLD` (default `1`), запустить дополнительный Perplexity low-yield collection и пересобрать report;
5. обновить daily/monthly wiki;
6. синхронизировать corpus и DOCX в `/mnt/vdd/Radar/data/corpus`.

### 5.2. Site publication layer

Entrypoint: `pipeline/bin/radar_daily_publish.sh`.

Последовательность:

1. `init_radar_db.py`;
2. полный `agpm_radar_docx_backfill.py` с metadata fetch текущей даты;
3. `agpm_radar_llm_classify.py`;
4. `agpm_radar_issue_theses.py`;
5. `agpm_radar_openclaw_analysis.py`;
6. `agpm_radar_quality.py`;
7. `agpm_radar_site_export.py`;
8. public production healthcheck.

Важно: full backfill удаляет и пересобирает material/rubric/quality/FTS/daily-stats наборы. Это не инкрементальная публикационная модель.

## 6. Legacy selection and fallback contract

Подтверждённые правила текущей реализации:

- daily limit: 10 материалов;
- прошедшие фильтр материалы сверх лимита пишутся в `daily-deferred.jsonl`;
- deferred имеет приоритет в следующем выпуске;
- исключаются ранее опубликованные canonical URL;
- есть отдельная event-level дедупликация для известных повторяющихся сюжетов;
- проверяются явные HTTP 404/410;
- выполняется title/content mismatch guard;
- для близкого периметра и сильных кандидатов выполняется fulltext second pass;
- применяется фильтр marketing/agent-wash и AgPM relevance;
- классификация рубрик имеет deterministic fallback;
- daily/period analysis имеет deterministic fallback;
- OpenClaw analysis использует primary model, fallback chain и retries;
- пустой выпуск считается допустимым;
- healthcheck проверяет наличие latest issue, согласованность material count, дубли latest issue, manual summary и ненулевые rubrics.

Текущая OpenClaw model chain в tracked default:

1. `openai/gpt-5.5`;
2. `openai/gpt-5.6-sol`;
3. `minimax/MiniMax-M3`.

Model IDs — Legacy baseline, не контракт V2.

## 7. Corpus and queues

Основной Project Manager corpus:

- `/root/.openclaw-projectmanager/workspace/knowledge/agpm-radar` — около 229 MiB, 355 файлов.

Синхронизированная Radar-копия:

- `/mnt/vdd/Radar/data/corpus/knowledge-agpm-radar` — около 229 MiB, 354 файла.

Operational inputs на момент снимка:

| Файл | Размер/строки | SHA-256 |
|---|---:|---|
| `data/materials.jsonl` | 24,301,813 bytes / 7,959 lines | `3a6e361c273b70621f1ea4beaa8591d037dd87eab245f418a9347269be71c568` |
| `data/daily-deferred.jsonl` | 0 bytes / 0 lines | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `sources.yml` | 11,303 bytes | `2e318b2b7c01031c5c5bd09fc5667662733a2e59bef424fbd9e943b981961389` |

Corpus inventory:

- 74 normalized issue JSON files;
- 74 parsed issue text files;
- 74 raw DOCX files;
- 75 top-level Markdown reports;
- 76 run artifacts;
- 473 source-metadata artifacts;
- 2,319 LLM-classification artifacts;
- monthly wiki snapshots for July and August 2026.

`config/agpm-radar.env` существует только в Project Manager workspace, имеет mode `0600`; значения не читались и не фиксировались.

## 8. SQLite baseline

Database: `/mnt/vdd/Radar/data/db/radar.sqlite`.

- Size: 4,194,304 bytes.
- SHA-256 at inventory: `481d5d6c9b54a58f78f288fb29c0eb072d43e74d6c2db8b14044a3153cd8f7f7`.
- `PRAGMA quick_check`: `ok`.
- `PRAGMA foreign_key_check`: 0 rows.
- Journal mode: `delete`.
- `user_version`: `0`.
- Migration registry: 6 migrations (`001`–`006`).
- Domain plus FTS tables: 24 total SQLite tables including FTS internals.

Key counts:

| Entity | Count |
|---|---:|
| issues | 74 |
| materials | 254 |
| daily_stats | 74 |
| issue_daily_analysis | 74 |
| issue_llm_theses | 4 |
| issue_period_theses | 148 |
| material_llm_summaries | 3 |
| material_rubrics | 728 |
| material_date_quality | 254 |
| llm_classifications | 254 |
| source_metadata | 244 |
| rejected_materials_internal | 7 |
| pipeline_runs | 0 |

Range: `2026-06-07` through `2026-08-19`.

Latest issue: `2026-08-19`, issue number 74, 3 materials.

Daily stats invariant failures: 0 for:

- `viewed = included + cut`;
- `included = near + mid + far`.

### Critical lifecycle gap

All 74 issue rows have:

- `status = 'draft'`;
- `published_at IS NULL`.

Nevertheless, all are treated as public by API/frontend. Therefore Legacy status cannot be trusted as published truth during import. Stage 1/3 must define an explicit historical-publication inference and V2 lifecycle contract.

### LLM states

- `issue_daily_analysis`: 70 deterministic fallback, 4 OpenClaw success;
- `issue_llm_theses`: 4 OpenClaw success;
- `material_llm_summaries`: 3 OpenClaw success.

This proves that Legacy frontend already depends on fallback-compatible data for most history.

## 9. API and frontend production

Public URL: `https://radar.aipractice.space/`.

At inventory time:

- frontend: HTTP 200, 10,363 bytes;
- API health: HTTP 200;
- latest issue: HTTP 200;
- `radar-api.service`: enabled and active;
- service restarts since current start: 0;
- API listener: `127.0.0.1:8765`;
- Caddy listeners: public 80/443;
- systemd daily timer: disabled and inactive to avoid competing with OpenClaw;
- production healthcheck for 2026-08-19 completed successfully.

Public API endpoints found in code:

- `/api/health`;
- `/api/latest` and `/api/issue/latest`;
- `/api/materials`;
- `/api/search`;
- `/api/stats`;
- `/api/internal/date-quality`;
- `/api/timeseries`;
- `/api/rubrics`;
- `/api/sources`;
- `/api/period-theses`;
- `/api/issues`;
- `/api/issue/{date}`.

Frontend directly calls latest, issue list, rubrics, sources and both issue/publication timeseries.

## 10. Known Legacy defects that V2 must not copy

These findings describe the current MVP; Stage 0 does not fix them.

1. API systemd service runs as `root`.
2. `systemd-analyze security radar-api.service` reports exposure `9.6 UNSAFE`.
3. API opens SQLite without `mode=ro` or `query_only`.
4. Public `/api/health` exposes the absolute DB path.
5. Public issue/material payloads expose report, DOCX, request/response and source absolute paths.
6. `/api/internal/date-quality` is publicly reachable without authentication.
7. Public payloads are built from `SELECT *`, not explicit DTO allowlists.
8. Invalid numeric parameters such as `limit=x` or `days=x` raise exceptions and return an empty connection response instead of a JSON 4xx.
9. `HEAD` on API paths returns 501 from the stdlib server.
10. API sends `Access-Control-Allow-Origin: *`.
11. No explicit public distinction exists between draft and published records.
12. Runtime is Python stdlib `ThreadingHTTPServer` with no explicit application-level request timeout, response budget or search rate limit.
13. The static export includes `internal/date-quality.json`; it is not currently served by the frontend root, but it must not enter V2 public artifacts.
14. Legacy DB is fully rebuilt from reports/corpus and is not the editorial source of truth.
15. Source code exists in both Project Manager workspace and Radar tree; copies match today but synchronization is procedural.
16. Git repository has no remote or release tags.
17. Live cron payload требует точный `openai/gpt-5.5`, четыре LLM-тезиса и LLM-summary каждой карточки, но фактический run 2026-08-19 успешно завершился через `minimax/MiniMax-M3`. Следовательно, декларативный cron contract и реальная fallback policy расходятся и зависят от рассуждения агента.
18. Активный cron живёт в SQLite state OpenClaw, а не в старых `cron/jobs.json*`; будущая интеграция должна использовать штатный cron API/state contract и проверять фактически активный instance.
19. Static root обслуживается прямо из worktree и содержит 43 ignored `*.bak*`-файла общим размером 977,080 bytes. Контрольный backup URL вернул HTTP 200 и byte-identical тело. Это активная утечка исходных/архивных артефактов; сами файлы не удаляются.
20. SPA fallback возвращает `index.html` с HTTP 200 даже для отсутствующих static assets и gazette names; asset/gazette 404 не отличим от SPA route.
21. Frontend экранирует текстовые поля, но вставляет `item.url` в `href` без attribute escaping и scheme allowlist. Текущие URL безопасны, но новый malicious source может создать HTML/script injection.

These defects are baseline acceptance tests for urgent Stage 0A and Stages 1, 3, 5, 7, 8, 12 and 15.

## 11. Gazette baseline

Current static gazette:

- `work/radar-app/gazette-20260803.html`;
- 36,237 bytes;
- SHA-256 `1e6ba2bb055a2821bca2e05ad7ef6ec57e3a558049875ffc5e601c58911b637d`;
- embedded by the frontend through a hardcoded filename;
- contains external Google Fonts dependencies;
- no versioned gazette manifest or release ledger exists.

Monthly source material also exists as `AgPM_gazette_source_2026-08.md` in the Project Manager corpus.

## 12. Backups and logs

Local backup evidence:

- 3 Radar tar archives under `backups/`, total 428,996,599 bytes;
- 15 SQLite backups under `data/db/backups/`, total 36,896,768 bytes;
- latest observed DB backup: before LLM fallback work on 2026-08-18;
- daily pipeline logs exist through 2026-08-19;
- Caddy access log contained 12,634 entries at inventory time.

Caddy status counts observed in the access log included 200, 304, 308, 404, 501 and 502 responses. Stage 0 does not infer SLOs from this historical log.

Off-host backup coverage of `/mnt/vdd/Radar` is not proven by this baseline and must be verified before production cutover.

## 13. Sanitized fixtures

Exporter:

- `tools/stage0/export_legacy_fixtures.py`;
- opens SQLite via URI `mode=ro`;
- sets `PRAGMA query_only=ON`;
- runs `PRAGMA quick_check`;
- selects explicit safe columns;
- excludes local paths, raw request/response artifacts, diagnostics, rejected materials and secrets.

Fixtures:

- normal latest issue: `2026-08-19`;
- deterministic fallback: `2026-08-15`;
- empty issue: `2026-07-26`;
- high-volume issue: `2026-08-04` with 16 materials.

Reproducibility check: two consecutive exports from unchanged DB produced byte-identical SHA-256 manifests and fixture files.

Fixture hashes are recorded in `fixtures/legacy-baseline/manifest.json`.

## 14. Stage 0 gate

- [x] Legacy code commit/tree fixed.
- [x] Project Manager cron owner/session/state proven.
- [x] SQLite schema/version/counts recorded.
- [x] Corpus/queues recorded.
- [x] Daily inputs/outputs and rules recorded.
- [x] API/frontend/Caddy/systemd recorded.
- [x] Gazette recorded.
- [x] Local backups/logs recorded.
- [x] Sanitized reproducible fixtures created.
- [x] Legacy production health green.
- [x] Legacy pipeline/runtime not changed.
- [x] Final Stage 0 repository diff classified for a dedicated commit.

The dedicated commit is the final Stage 0 handoff artifact.

## 15. Влияние Stage 0 на дальнейший план

Новые факты не меняют целевую архитектуру, но требуют urgent containment и уточняют дальнейший порядок:

1. **Urgent Stage 0A:** до начала V2-разработки закрыть public access к backup/temp artifacts в Legacy static root, ничего не удаляя; после graceful Caddy reload доказать блокировку и сохранность основного UI/API.
2. **Stage 1:** publication lifecycle должен быть определён независимо от Legacy `issues.status`; исторически публичные строки импортируются по явному inference contract, а исходные Legacy status/published_at сохраняются как provenance.
3. **Stage 3:** importer не имеет права механически переносить `draft` как V2 lifecycle state. Он должен доказуемо распознать 74 исторически публичных выпуска и отдельно импортировать реальные редакционные drafts/queues.
4. **Stage 5:** Project Manager report contract фиксирует запрошенную модель, фактически использованную модель, цепочку попыток и итоговый fallback/no-LLM status. Успешный fallback не считается publication failure.
5. **Stage 8:** обязательны regression cases покрывают explicit DTO, отсутствие path/draft/internal leakage, URL attribute/scheme validation, корректные asset 404 и JSON 4xx для невалидных параметров.
6. **Stage 15:** cron меняется через реальный Project Manager OpenClaw instance и штатный cron mechanism; нельзя редактировать устаревшие JSON-файлы. Сохраняется внешний порядок `build/validate DOCX -> delivery -> Legacy publish -> V2 publish -> combined final report`.
