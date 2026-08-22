#!/usr/bin/env bash
# Apply every migration to a throwaway database and run the SQL-backed tests.
#
# scripts/verify.sh is the gate and runs anywhere, so it deliberately skips the
# tests that need a server. This script is the other half: it proves the SQL
# actually applies - to the repository baseline and to the schema production
# drifted into - before anything is proposed for production.
#
# Requirements: a local PostgreSQL 16+ with pgcrypto, pg_trgm, unaccent and
# pgvector, reachable by a role that may CREATE DATABASE and CREATE EXTENSION.
# Override the connection with RADAR_KX_TEST_ADMIN_DSN.
#
# It applies 003 and, in its own fixture, 004 - written and verified, awaiting
# the owner's decision to apply it.
#
# It never touches Radar KX production: the DSN is local, the databases it
# creates are named radar_kx_test_*, and it drops them when it is done.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
kx_root="$(cd -- "${script_dir}/.." && pwd)"

cd "${kx_root}"

export RADAR_KX_TEST_ADMIN_DSN="${RADAR_KX_TEST_ADMIN_DSN:-dbname=postgres}"

case "${RADAR_KX_TEST_ADMIN_DSN}" in
  *radar_kx[^_]*|*147.45.99.225*)
    printf 'refusing to run against what looks like production: %s\n' \
      "${RADAR_KX_TEST_ADMIN_DSN}" >&2
    exit 1
    ;;
esac

printf '[verify-migrations] admin DSN: %s\n' "${RADAR_KX_TEST_ADMIN_DSN}"

uv sync --locked --python 3.12 --group dev
uv run --no-sync pytest tests/test_migration_003.py tests/test_artifact_import.py \
  tests/test_canon_corpus.py tests/test_search.py -q

printf 'Radar KX migration verification: PASS\n'
