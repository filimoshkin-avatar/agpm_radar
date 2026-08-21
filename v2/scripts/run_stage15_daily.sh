#!/usr/bin/env bash
set -euo pipefail

v2_root="/mnt/vdd/Radar/v2"
cd "$v2_root"
export PYTHONPATH="$v2_root"
"${v2_root}/.venv/bin/python" "${v2_root}/tools/check_legacy_mirror.py" \
  --repository-scripts /mnt/vdd/Radar/pipeline/scripts \
  --runtime-scripts /root/.openclaw-projectmanager/workspace/scripts >&2
requested_issue_date="${RADAR_ISSUE_DATE:-$(TZ=Europe/Moscow date +%F)}"
issue_date="$requested_issue_date"
legacy_json="/mnt/vdd/Radar/data/exports/json-cache/issues/${issue_date}.json"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
finished_at="$(date -u -d '+1 minute' +%Y-%m-%dT%H:%M:%SZ)"

if [[ ! -f "$legacy_json" ]]; then
  for _ in {1..60}; do
    sleep 10
    [[ -f "$legacy_json" ]] && break
  done
fi

if [[ ! -f "$legacy_json" && -z "${RADAR_ISSUE_DATE:-}" ]]; then
  if ! issue_date="$(${v2_root}/.venv/bin/python "${v2_root}/tools/find_stage15_catchup.py" \
      --exports-root /mnt/vdd/Radar/data/exports/json-cache/issues \
      --runs-root /root/.openclaw-projectmanager/workspace/state/radar-v2/dual-run-cron \
      --through "$requested_issue_date" --lookback-days 7)"; then
    issue_date="$requested_issue_date"
  fi
  legacy_json="/mnt/vdd/Radar/data/exports/json-cache/issues/${issue_date}.json"
fi

if [[ ! -f "$legacy_json" ]]; then
  echo "Radar V2 dual-run: Legacy выпуск ${requested_issue_date} ещё не опубликован, незакрытого выпуска за 7 дней нет." >&2
  exit 2
fi

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
  --ssh-host radar-v2-deploy@147.45.99.225 \
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

if [[ "$(${v2_root}/.venv/bin/python -c 'import json,sys; print(str(json.loads(sys.argv[1]).get("comparisonVerdict", {}).get("alert", False)).lower())' "$report")" == "true" ]]; then
  exit 3
fi
