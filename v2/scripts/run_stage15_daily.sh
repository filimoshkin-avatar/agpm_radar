#!/usr/bin/env bash
set -euo pipefail

v2_root="/mnt/vdd/Radar/v2"
cd "$v2_root"
issue_date="${RADAR_ISSUE_DATE:-$(TZ=Europe/Moscow date +%F)}"
legacy_json="/mnt/vdd/Radar/data/exports/json-cache/issues/${issue_date}.json"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
finished_at="$(date -u -d '+1 minute' +%Y-%m-%dT%H:%M:%SZ)"

if [[ ! -f "$legacy_json" ]]; then
  echo "Radar V2 dual-run: Legacy выпуск ${issue_date} ещё не опубликован." >&2
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
  --application-release-id app_release_20260820_10fc9c8 \
  --v2-public-base https://radar.agpm.space)"

python3 -c '
import json, sys
x = json.load(sys.stdin)
legacy = x["legacy"]
v2 = x["v2"]
diff = x["urlDifferences"]
print(
    f"Radar dual-run {x['"'"'issueDate'"'"']}: Legacy={legacy['"'"'materialCount'"'"']} материалов, "
    f"V2={v2['"'"'materialCount'"'"']} материалов, release={v2['"'"'releaseId'"'"']}, "
    f"publication={x['"'"'publication'"'"']['"'"'disposition'"'"']}, "
    f"onlyLegacy={len(diff['"'"'onlyLegacy'"'"'])}, onlyV2={len(diff['"'"'onlyV2'"'"'])}."
)
' <<<"$report"
