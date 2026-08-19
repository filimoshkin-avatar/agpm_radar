# Radar V2 contract family v1

Status: accepted by Stage 1.

This directory is the machine-readable boundary between Project Manager, publisher, source SQLite, Local Ru activator, public API and release automation.

Files:

- `candidate.schema.json` — daily/correction/gazette candidate manifest;
- `delta.schema.json` — publisher-generated row-level content delta;
- `publisher-result.schema.json` — authoritative machine result;
- `project-manager-report.schema.json` — required final user-report payload;
- `compatibility-manifest.schema.json` — application/content/gazette compatibility;
- `sqlite-contract.yaml` — runtime, pragmas, tables, columns, writers and mutation allowlist;
- `historical-publication-inference.yaml` — Legacy import evidence rules;
- `publisher-state-machine.yaml` — durable states, transitions and exit codes;
- `error-taxonomy.yaml` — stable error semantics;
- `public-api.openapi.yaml` — published-only read-only API;
- `examples/` — valid contract examples.

Rules:

1. Contract versions are semantic strings. Breaking changes require a new contract family directory.
2. Unknown properties are rejected in JSON package schemas.
3. Candidate and delta packages are data only: no SQL, DDL, migrations, commands, secrets or host paths.
4. Project Manager creates candidates; only publisher creates deltas/results.
5. Public API DTOs never mirror internal tables and never expose drafts.
6. Run validation with:

```bash
python3 tools/contracts/validate_contracts.py
```
