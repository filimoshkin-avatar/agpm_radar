#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
kx_root="$(cd -- "${script_dir}/.." && pwd)"

cd "${kx_root}"

uv sync --locked --python 3.12 --group dev
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy
uv run --no-sync pytest
uv export --format requirements.txt --no-header --no-dev --no-emit-project --locked \
  | diff -u deploy/requirements.lock -
git -C "${kx_root}/.." diff --check

printf 'Radar KX verification: PASS\n'
