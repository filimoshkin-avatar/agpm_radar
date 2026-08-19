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

step "secret and Legacy-isolation scan"
uv run --no-sync python tools/check_isolation.py

step "deterministic production artifact and manifest"
uv run --no-sync python tools/build_production_artifact.py --check

printf '\nRadar V2 Stage 2 verification: PASS\n'
