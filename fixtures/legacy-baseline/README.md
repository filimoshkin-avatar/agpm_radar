# Legacy Radar baseline fixtures

These fixtures capture sanitized, deterministic projections of selected Legacy Radar issues for Radar V2 migration and golden tests.

Cases:

- `normal-latest-2026-08-19.json` — latest issue with OpenClaw LLM output and material summaries;
- `deterministic-fallback-2026-08-15.json` — issue using the deterministic daily-analysis fallback and no `issue_llm_theses` row;
- `empty-issue-2026-07-26.json` — published-on-the-site Legacy issue with zero materials;
- `high-volume-2026-08-04.json` — issue with 16 materials, above the later daily limit of 10;
- `manifest.json` — source DB fingerprint, migrations, counts, fixture hashes and sanitization declaration.

The files intentionally exclude local filesystem paths, raw provider request/response files, diagnostic JSON, rejected-material records, secrets and OAuth data.

Reproduce from an unchanged Legacy DB:

```bash
python3 tools/stage0/export_legacy_fixtures.py \
  --db /mnt/vdd/Radar/data/db/radar.sqlite \
  --out fixtures/legacy-baseline
```

The exporter opens SQLite with `mode=ro` and `PRAGMA query_only=ON`. Given the same database bytes, it produces byte-identical fixture JSON.
