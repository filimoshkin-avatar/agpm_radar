#!/usr/bin/env bash
set -euo pipefail

cd /root/.openclaw-projectmanager/workspace

if [[ -f config/agpm-radar.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source config/agpm-radar.env
  set +a
fi

run_id="$(TZ=Europe/Moscow date +%F)"
log_dir="logs"
mkdir -p "$log_dir"
radar_root="${RADAR_ROOT:-/mnt/vdd/Radar}"
radar_corpus="$radar_root/data/corpus/knowledge-agpm-radar"
radar_raw_docx="$radar_root/data/corpus/raw-docx"

{
  echo "[$(date -Is)] AgPM radar collection started"
  python3 scripts/agpm_radar_collect.py --run-id "$run_id"
  echo "[$(date -Is)] AgPM radar collection finished"
  echo "[$(date -Is)] AgPM daily radar report generation started"
  report_json="$(python3 scripts/agpm_radar_report.py --days 1 --until "$run_id" --output-prefix daily)"
  echo "$report_json"
  included_count="$(python3 -c 'import json,sys; print(int((json.load(sys.stdin).get("included") or 0)))' <<< "$report_json")"
  low_yield_threshold="${RADAR_LOW_YIELD_THRESHOLD:-1}"
  if (( included_count <= low_yield_threshold )); then
    echo "[$(date -Is)] AgPM daily radar low-yield fallback started: included=$included_count threshold=$low_yield_threshold"
    python3 scripts/agpm_radar_collect.py \
      --run-id "$run_id-perplexity-expansion" \
      --web-research-only \
      --auxiliary-run \
      --web-provider perplexity \
      --query-set low_yield_expansion
    echo "[$(date -Is)] AgPM daily radar report regeneration after Perplexity expansion started"
    report_json="$(python3 scripts/agpm_radar_report.py --days 1 --until "$run_id" --output-prefix daily)"
    echo "$report_json"
    included_count="$(python3 -c 'import json,sys; print(int((json.load(sys.stdin).get("included") or 0)))' <<< "$report_json")"
    echo "[$(date -Is)] AgPM daily radar low-yield fallback finished: included=$included_count"
  fi
  python3 - "$run_id" "$included_count" <<'PY'
import json
import sys
from pathlib import Path

path = Path("knowledge/agpm-radar/data/state.json")
state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
state["last_issue_date"] = sys.argv[1]
state["last_report_included"] = int(sys.argv[2])
path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  echo "[$(date -Is)] AgPM daily radar report generation finished"
  echo "[$(date -Is)] AgPM radar wiki statistics update started"
  python3 scripts/agpm_radar_wiki.py --until "$run_id"
  echo "[$(date -Is)] AgPM radar wiki statistics update finished"

  echo "[$(date -Is)] AgPM radar corpus sync to site workspace started"
  mkdir -p "$radar_corpus" "$radar_raw_docx"
  rsync -a knowledge/agpm-radar/ "$radar_corpus/"
  find knowledge/agpm-radar/reports -maxdepth 1 -type f -name 'AgPM_*_radar_*.docx' -exec cp -p {} "$radar_raw_docx/" \;
  echo "[$(date -Is)] AgPM radar corpus sync to site workspace finished"
} >> "$log_dir/agpm_radar_daily.log" 2>&1
