#!/usr/bin/env bash
set -euo pipefail

v2_root="/mnt/vdd/Radar/v2"
cd "$v2_root"
export PYTHONPATH="$v2_root"
"${v2_root}/.venv/bin/python" "${v2_root}/tools/check_legacy_mirror.py" \
  --repository-scripts /mnt/vdd/Radar/pipeline/scripts \
  --runtime-scripts /root/.openclaw-projectmanager/workspace/scripts >&2
# The first date this daily contour owns. Legacy issues older than this were published
# outside the dual-run boundary and must never be selected as a catch-up target.
daily_contour_start="2026-08-20"
requested_issue_date="${RADAR_ISSUE_DATE:-$(TZ=Europe/Moscow date +%F)}"
issue_date="$requested_issue_date"
legacy_json="/mnt/vdd/Radar/data/exports/json-cache/issues/${issue_date}.json"

# Bounded wait so a late Legacy run still fits inside the cron timeout budget alongside
# candidate build (300s), SSH transport (60s) and public convergence retries (15s).
if [[ ! -f "$legacy_json" ]]; then
  for _ in {1..30}; do
    sleep 10
    [[ -f "$legacy_json" ]] && break
  done
fi

if [[ ! -f "$legacy_json" && -z "${RADAR_ISSUE_DATE:-}" ]]; then
  if ! issue_date="$(${v2_root}/.venv/bin/python "${v2_root}/tools/find_stage15_catchup.py" \
      --exports-root /mnt/vdd/Radar/data/exports/json-cache/issues \
      --runs-root /root/.openclaw-projectmanager/workspace/state/radar-v2/dual-run-cron \
      --through "$requested_issue_date" --not-before "$daily_contour_start" \
      --lookback-days 7)"; then
    issue_date="$requested_issue_date"
  fi
  legacy_json="/mnt/vdd/Radar/data/exports/json-cache/issues/${issue_date}.json"
fi

if [[ ! -f "$legacy_json" ]]; then
  echo "Radar V2 dual-run: Legacy выпуск ${requested_issue_date} ещё не опубликован, незакрытого выпуска за 7 дней нет." >&2
  exit 2
fi

# Captured only once the Legacy input exists, so a wait never backdates the retained provenance.
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
finished_at="$(date -u -d '+1 minute' +%Y-%m-%dT%H:%M:%SZ)"

report="$(${v2_root}/.venv/bin/python -m tools.run_stage15_dual \
  --issue-date "$issue_date" \
  --started-at "$started_at" \
  --finished-at "$finished_at" \
  --legacy-json "$legacy_json" \
  --legacy-db /mnt/vdd/Radar/data/db/radar.sqlite \
  --source-root /root/.openclaw-projectmanager/workspace/state/radar-v2/source \
  --publisher-root /root/.openclaw-projectmanager/workspace/state/radar-v2/publisher \
  --runs-root /root/.openclaw-projectmanager/workspace/state/radar-v2/dual-run-cron \
  --v2-root "$v2_root" \
  --python "$v2_root/.venv/bin/python" \
  --ssh-host radar-v2-deploy@radar.agpm.space \
  --ssh-identity /root/.ssh/radar_v2_publisher_stage13 \
  --v2-public-base https://radar.agpm.space)"

summary="$(${v2_root}/.venv/bin/python - "$report" <<'PY'
import json, sys
x = json.loads(sys.argv[1])
legacy = x["legacy"]
v2 = x["v2"]
diff = x["urlDifferences"]
verdict = x.get("comparisonVerdict", {"status": "legacy_report_without_verdict", "alert": False})
print(
    f"Radar dual-run {x['issueDate']}: Legacy={legacy['materialCount']} материалов, "
    f"V2={v2['materialCount']} материалов, release={v2['releaseId']}, "
    f"publication={x['publication']['disposition']}, "
    f"onlyLegacy={len(diff['onlyLegacy'])}, onlyV2={len(diff['onlyV2'])}, "
    f"verdict={verdict['status']}."
)
PY
)"
printf '%s\n' "$summary"

notify_message="$(${v2_root}/.venv/bin/python - "$report" <<'PY'
import json
import sys

x = json.loads(sys.argv[1])
legacy = x["legacy"]
v2 = x["v2"]
diff = x["urlDifferences"]
verdict = x.get("comparisonVerdict", {"status": "legacy_report_without_verdict", "alert": False})
publication = x["publication"]
release_id = v2.get("releaseId") or "нет release id"
issue_date = x["issueDate"]
only_legacy = diff.get("onlyLegacy", [])
only_v2 = diff.get("onlyV2", [])
periods = v2.get("periodAnalysis", {})

status = "опубликован" if v2.get("status") == "published" else str(v2.get("status", "неизвестно"))
publication_disposition = publication.get("disposition", "unknown")
verdict_status = verdict.get("status", "unknown")
alert = bool(verdict.get("alert", False))

if alert:
    verdict_line = "Требуется внимание: расхождение Legacy/V2 не объяснено автоматически."
elif only_legacy or only_v2:
    verdict_line = "Расхождения Legacy/V2 есть, но они объяснены правилами V2."
else:
    verdict_line = "Расхождений между Legacy и V2 нет."

lines = [
    f"Радар V2: выпуск за {issue_date} обработан.",
    "",
    f"Статус V2: {status}.",
    f"Публикация: {publication_disposition}.",
    f"Release: {release_id}.",
    f"Legacy: {legacy['materialCount']} материалов.",
    f"V2: {v2['materialCount']} материалов.",
    f"LLM V2: {v2.get('llmStatus', 'unknown')}.",
    (
        "Период 7 дней: "
        f"{periods.get('7d', {}).get('status', 'missing')}, "
        f"модель {periods.get('7d', {}).get('model') or 'нет'}, "
        f"попыток {periods.get('7d', {}).get('attempts', 0)}."
    ),
    (
        "Период 30 дней: "
        f"{periods.get('30d', {}).get('status', 'missing')}, "
        f"модель {periods.get('30d', {}).get('model') or 'нет'}, "
        f"попыток {periods.get('30d', {}).get('attempts', 0)}."
    ),
    f"Только в Legacy: {len(only_legacy)}.",
    f"Только в V2: {len(only_v2)}.",
    f"Вердикт сверки: {verdict_status}.",
    verdict_line,
    "",
    f"Выпуск: https://radar.agpm.space/issues/{issue_date}",
    f"API: https://radar.agpm.space/api/issues/{issue_date}",
]

for label, key in (("7 дней", "7d"), ("30 дней", "30d")):
    period = periods.get(key, {})
    if period.get("status") == "fallback":
        lines.append(f"Fallback {label}: {period.get('error') or 'причина не указана'}")

if only_legacy:
    lines.extend(["", "Не попали в V2:"])
    lines.extend(f"- {url}" for url in only_legacy[:5])
    if len(only_legacy) > 5:
        lines.append(f"- и ещё {len(only_legacy) - 5}")

if only_v2:
    lines.extend(["", "Есть только в V2:"])
    lines.extend(f"- {url}" for url in only_v2[:5])
    if len(only_v2) > 5:
        lines.append(f"- и ещё {len(only_v2) - 5}")

print("\n".join(lines))
PY
)"

if [[ -n "${RADAR_V2_NOTIFY_CHANNEL:-}" && -n "${RADAR_V2_NOTIFY_TARGET:-}" ]]; then
  if ! openclaw message send \
      --channel "$RADAR_V2_NOTIFY_CHANNEL" \
      --target "$RADAR_V2_NOTIFY_TARGET" \
      --message "$notify_message" >/dev/null; then
    echo "Radar V2 notification delivery failed." >&2
  fi
else
  echo "Radar V2 notification skipped: RADAR_V2_NOTIFY_CHANNEL or RADAR_V2_NOTIFY_TARGET is not set." >&2
fi

if [[ "$(${v2_root}/.venv/bin/python -c 'import json,sys; print(str(json.loads(sys.argv[1]).get("comparisonVerdict", {}).get("alert", False)).lower())' "$report")" == "true" ]]; then
  exit 3
fi
