# Radar Stage 0A: Caddy containment evidence

Дата: 2026-08-19

Статус: completed

## 1. Причина

Stage 0 доказал, что Legacy Radar раздаётся непосредственно из mutable worktree `/mnt/vdd/Radar/work/radar-app`. В static root находились 43 ignored backup-файла общим размером 977,080 bytes. Representative `.bak` files и URL с encoded dot возвращали HTTP 200. Отсутствующий source map попадал в SPA fallback и также возвращал index.html с HTTP 200.

Stage 0A закрывает только backup/temp/source-map artifacts. Файлы не удаляются, Legacy pipeline/API/frontend не изменяются.

## 2. Изменение

В Caddy vhost `radar.aipractice.space` перед `/api/*` и SPA fallback добавлен fail-closed matcher:

```caddy
@radar_private_artifacts path_regexp radar_private_artifacts (?i)(?:^|/)[^/]*(?:\.bak(?:\.[^/]*)?|\.backup(?:\.[^/]*)?|\.old(?:\.[^/]*)?|\.orig(?:\.[^/]*)?|\.tmp(?:\.[^/]*)?|\.temp(?:\.[^/]*)?|\.sw[op]|~|\.map)$

handle @radar_private_artifacts {
	respond 404
}
```

Тот же matcher добавлен в tracked template `deploy/Caddyfile.radar.aipractice.space`.

## 3. Backup и evidence

- Active Caddyfile before SHA-256: `23fc2e9980c8dac2b4918de73363d17802bb3978621dc885d2fb3c5e9081a08c`.
- Active Caddyfile after SHA-256: `5089ce201b2a2ab82455983aab77d3cc87b8fa24808cc037dbc3fbc2c92dd45a`.
- Backup: `/etc/caddy/Caddyfile.backup-before-radar-stage0a-20260819T131549Z`.
- Evidence directory: `/root/radar-stage0a-evidence-20260819T131549Z`.
- Backup checksum equals the original active checksum.
- Candidate был адаптирован и валидирован до замены active config.

## 4. Reload

- Operation: graceful `systemctl reload caddy.service`.
- Main PID before/after: `259034` / `259034`.
- `NRestarts` before/after: `0` / `0`.
- Final state: enabled, active, running.
- Final `caddy validate`: PASS.
- Warning/error-priority journal entries after reload: none.

## 5. Negative checks

Все 43 фактически существующих backup artifacts проверены по публичным URL и возвращают HTTP 404.

Дополнительные checks, HTTP 404:

- exact `.bak.before-*`;
- percent-encoded dot `%2e`;
- case-insensitive `%2EBAK`;
- encoded suffix separator;
- `.map` и encoded `.map`;
- `.tmp`, `.temp`, `.old`, `.orig`;
- `.swp`, `.swo`, trailing `~`;
- matching artifact path под `/api/`.

## 6. Positive regression checks

HTTP 200 после reload:

- `/`;
- active `app.js`;
- active `styles.css`;
- `favicon.svg`;
- `gazette-20260803.html`;
- `/api/health`;
- `/api/latest`.

Официальный `/mnt/vdd/Radar/pipeline/bin/radar_healthcheck.sh --production`: PASS, latest issue `2026-08-19`, 3 materials.

Unrelated hosts сохранили baseline HTTP 200:

- `aipractice.space`;
- `aipmo-pp.aipractice.space`;
- `tea.aipractice.space`.

## 7. Retention and non-impact

- Backup/temp files retained: 43.
- Retained bytes: 977,080.
- Retained artifact manifest SHA-256: `4958a4bc4f1c02035ca09ce9a228e7c210815ca997faddb6c4163d93c6eb3517`.
- Legacy SQLite SHA-256 unchanged: `481d5d6c9b54a58f78f288fb29c0eb072d43e74d6c2db8b14044a3153cd8f7f7`.
- Legacy API source/frontend hashes unchanged.
- `radar-api.service` remained active with same PID and `NRestarts=0`.
- No file was deleted.

## 8. Remaining known behavior

Stage 0A intentionally does not fix the generic SPA fallback: an unrelated missing asset such as `/missing-asset.js` can still return index.html with HTTP 200. This remains an explicit Stage 8 regression requirement together with API DTO/path/draft/internal leakage and invalid-parameter JSON 4xx behavior.

## 9. Rollback

If rollback is required:

```bash
cp -a /etc/caddy/Caddyfile.backup-before-radar-stage0a-20260819T131549Z /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy.service
```

Rollback was not needed.

## 10. Plan impact

Stage 0A confirms the current Caddy 2.6.2 matcher/handle ordering and closes the urgent public artifact exposure without changing Legacy behavior. No additional stage is required. Existing Stage 8 must retain the stricter missing-asset and public-boundary tests. The next sequential stage is Stage 1: architecture contracts and ADRs.
