#!/usr/bin/env bash
set -euo pipefail

export RADAR_ROOT="${RADAR_ROOT:-/mnt/vdd/Radar}"
export RADAR_DB="${RADAR_DB:-$RADAR_ROOT/data/db/radar.sqlite}"

cd "$RADAR_ROOT/pipeline/scripts"

if [[ -f "$RADAR_ROOT/pipeline/config/radar.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$RADAR_ROOT/pipeline/config/radar.env"
  set +a
fi

log_dir="$RADAR_ROOT/data/logs/pipeline"
mkdir -p "$log_dir"
run_id="$(TZ=Europe/Moscow date +%F)"
log_file="$log_dir/radar_daily_publish_${run_id}.log"

{
	  echo "[$(date -Is)] Radar publish started"
	  python3 init_radar_db.py
	  python3 agpm_radar_docx_backfill.py --fetch-metadata --fetch-metadata-issue-date "$run_id" --sleep 0.05
	  python3 agpm_radar_llm_classify.py
	  python3 agpm_radar_issue_theses.py
	  python3 agpm_radar_openclaw_analysis.py --issue-date "$run_id"
	  python3 agpm_radar_quality.py
	  python3 agpm_radar_site_export.py
	  "$RADAR_ROOT/pipeline/bin/radar_healthcheck.sh" --production
	  echo "[$(date -Is)] Radar publish finished"
} >> "$log_file" 2>&1

echo "$log_file"
