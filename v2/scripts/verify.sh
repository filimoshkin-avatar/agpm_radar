#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
v2_root="$(cd -- "${script_dir}/.." && pwd)"
repository_root="$(cd -- "${v2_root}/.." && pwd)"

cd "${v2_root}"

step() {
  printf '\n[verify] %s\n' "$1"
}

step "locked Python 3.12 development sync"
uv sync --locked --python 3.12 --group dev

step "Ruff format check"
uv run --no-sync ruff format --check .

step "Ruff lint"
uv run --no-sync ruff check .

step "strict mypy"
uv run --no-sync mypy

step "pytest"
uv run --no-sync pytest

step "parent Stage 1 contract validator"
uv run --no-sync python "${repository_root}/tools/contracts/validate_contracts.py"

step "dependency-free web ES module syntax"
mapfile -d '' javascript_files < <(find apps/web -type f \( -name '*.js' -o -name '*.mjs' \) -print0)
if ((${#javascript_files[@]} == 0)); then
  printf 'No JavaScript modules found.\n' >&2
  exit 1
fi
for javascript_file in "${javascript_files[@]}"; do
  node --check "${javascript_file}"
done
printf 'JavaScript syntax: PASS (%d module(s))\n' "${#javascript_files[@]}"
node --check "${repository_root}/work/radar-app/app.js"
printf 'Legacy JavaScript syntax: PASS\n'

step "frontend console smoke"
node tools/frontend_console_smoke.mjs
# The same smoke against a pre-contract issue, in a second process:
# `app.mjs` caches the issue it holds, so a second payload in one process
# is a race rather than a test.
node tools/frontend_console_smoke.mjs --pre-contract
# And a third: the rubrics endpoint answers 503. That panel is secondary, so
# the issue must still render rather than fall to the error banner.
node tools/frontend_console_smoke.mjs --rubrics-down
# And a fourth: a reader arriving by /issues/<date> must get that issue, not
# the latest - the address is read at boot and written back canonically.
node tools/frontend_console_smoke.mjs --deep-link
# A date that is not a date, and a date that is real but never published: both
# open the latest issue instead of locking the page in a retry loop or leaving
# a blank screen.
node tools/frontend_console_smoke.mjs --dead-link
node tools/frontend_console_smoke.mjs --absent-link
# And the gazette: the archive, the header line and the frame come from
# /api/gazettes, and none of that code ran in any smoke before.
node tools/frontend_console_smoke.mjs --gazette

step "agent view console smoke"
node tools/agent_console_smoke.mjs

step "review regressions: history, search and recovery"
node tools/frontend_recovery_smoke.mjs

step "Legacy and V2 out-of-order reload regression"
node "${repository_root}/tools/frontend_period_switch_race_smoke.mjs"

step "asset cache tokens"
python3 tools/check_asset_tokens.py

step "design-system debt"
python3 tools/check_design_rules.py

step "secret and Legacy-isolation scan"
uv run --no-sync python tools/check_isolation.py

step "deterministic production artifact and manifest"
uv run --no-sync python tools/build_production_artifact.py --check

printf '\nRadar V2 verification: PASS\n'
