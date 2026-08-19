#!/usr/bin/env bash
set -euo pipefail

MODE="${RADAR_HEALTHCHECK_MODE:-production}"

usage() {
  cat <<'EOF'
Usage: radar_healthcheck.sh [--production|--local]

Modes:
  --production  Check the public Radar site through Caddy. Default.
  --local       Check a local development frontend and backend.

Environment overrides:
  RADAR_BASE_URL   Base public URL for production mode.
  RADAR_API_URL    API URL override.
  RADAR_FRONT_URL  Frontend URL override.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --production)
      MODE="production"
      ;;
    --local)
      MODE="local"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

case "$MODE" in
  production)
    BASE_URL="${RADAR_BASE_URL:-https://radar.aipractice.space}"
    API_URL="${RADAR_API_URL:-$BASE_URL}"
    FRONT_URL="${RADAR_FRONT_URL:-$BASE_URL}"
    ;;
  local)
    API_URL="${RADAR_API_URL:-http://127.0.0.1:8765}"
    FRONT_URL="${RADAR_FRONT_URL:-http://127.0.0.1:8780}"
    ;;
  *)
    echo "Unknown RADAR_HEALTHCHECK_MODE: $MODE" >&2
    usage >&2
    exit 2
    ;;
esac

api_code="$(curl -sS -o /tmp/radar_health_api.json -w '%{http_code}' "$API_URL/api/health" || true)"
if [[ "$api_code" != "200" ]]; then
  echo "API health failed: HTTP $api_code"
  exit 1
fi

latest_code="$(curl -sS -o /tmp/radar_latest.json -w '%{http_code}' "$API_URL/api/issue/latest" || true)"
if [[ "$latest_code" != "200" ]]; then
  echo "Latest issue failed: HTTP $latest_code"
  exit 1
fi

rubrics_code="$(curl -sS -o /tmp/radar_rubrics.json -w '%{http_code}' "$API_URL/api/rubrics?period=30d" || true)"
if [[ "$rubrics_code" != "200" ]]; then
  echo "Rubrics failed: HTTP $rubrics_code"
  exit 1
fi

python3 - <<'PY'
import json
import re
from collections import defaultdict
from pathlib import Path

payload = json.loads(Path("/tmp/radar_latest.json").read_text(encoding="utf-8"))
issue = payload.get("issue") or {}
materials = payload.get("materials") or []
if not issue.get("issue_date"):
    raise SystemExit("Latest issue has no issue_date")
if not materials:
    issue_stats = payload.get("issue_stats") or {}
    included = int(issue_stats.get("included") or 0)
    if included:
        raise SystemExit(f"Latest issue has no public materials, but issue_stats.included={included}")
    print(f"Latest issue {issue.get('issue_date')} materials=0 (empty issue accepted)")
    raise SystemExit(0)
print(f"Latest issue {issue.get('issue_date')} materials={len(materials)}")

def fingerprint(value: str, max_tokens: int = 14) -> str:
    normalized = re.sub(r"[^0-9a-zа-яё]+", " ", (value or "").lower()).strip()
    tokens = [token for token in normalized.split() if len(token) > 1]
    return " ".join(tokens[:max_tokens])

by_canonical = defaultdict(list)
by_title = defaultdict(list)
for material in materials:
    canonical = (material.get("canonical_url") or material.get("url") or "").rstrip("/")
    if canonical:
        by_canonical[canonical].append(material)
    title_key = fingerprint(material.get("title") or "")
    if title_key:
        by_title[title_key].append(material)

canonical_dupes = {key: rows for key, rows in by_canonical.items() if len(rows) > 1}
title_dupes = {key: rows for key, rows in by_title.items() if len(rows) > 1}
if canonical_dupes or title_dupes:
    examples = []
    for rows in list(canonical_dupes.values()) + list(title_dupes.values()):
        examples.append(" | ".join(row.get("title") or row.get("url") or "untitled" for row in rows[:3]))
    raise SystemExit("Latest issue has possible duplicate public materials: " + "; ".join(examples[:3]))

weak_manual = []
for material in materials:
    source_id = str(material.get("source_id") or "")
    summary = material.get("summary") or ""
    if source_id.startswith("manual_") and len(summary) < 300:
        weak_manual.append(material.get("title") or material.get("url") or source_id)
if weak_manual:
    raise SystemExit("Manual source has too short public summary: " + "; ".join(weak_manual[:3]))

rubrics = json.loads(Path("/tmp/radar_rubrics.json").read_text(encoding="utf-8")).get("rubrics") or []
if not any((rubric.get("count") or 0) > 0 for rubric in rubrics):
    raise SystemExit("30d rubrics have no materials")
PY

front_code="$(curl -sS -o /tmp/radar_front.html -w '%{http_code}' "$FRONT_URL/" || true)"
if [[ "$front_code" != "200" ]]; then
  echo "Frontend failed: HTTP $front_code"
  exit 1
fi

if ! grep -q 'Радар AgPM' /tmp/radar_front.html; then
  echo "Frontend HTML does not contain expected title"
  exit 1
fi

echo "Radar healthcheck OK ($MODE)"
