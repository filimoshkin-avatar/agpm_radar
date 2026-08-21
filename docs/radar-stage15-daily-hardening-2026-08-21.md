# Radar V2 Stage 15 daily hardening — 2026-08-21

## Scope

This change hardens the already accepted Stage 15 daily boundary without changing Legacy selection
rules, V2 domain selection rules, database schema, public API contracts, Caddy, DNS or service unit
configuration.

The public health contract now includes `applicationReleaseId`, read at API startup from the
immutable `APPLICATION-RELEASE.json` installed beside the active API artifact. This is the runtime
pointer source of truth and deliberately differs from querying the latest append-only compatibility
history row.

## Invariants

- `combined-report.json` is the only completion marker. An incomplete date is retryable and each
  failed attempt remains retained under a create-only `attempt-NNN` directory.
- Remote publication idempotency inputs remain stable. The comparison report's `generatedAt` is an
  actual completion timestamp and is not part of the publisher idempotency key.
- Public convergence is bounded to five attempts with three-second spacing and a clear terminal
  error.
- URL differences plus shared-material editorial fields (title, summaries, takeaway, theses,
  trend, perimeter, verdict, signal, rubrics and key flag) are classified as `matched`, `explained`,
  or `unexplained`. An unexplained result is printed and makes the launcher exit non-zero for the
  scheduler alert path.
- LLM success is accepted only from an explicit status field; schema shape alone never implies
  success.
- The application release ID comes from the live runtime marker exposed by public health, not a
  shell literal or the append-only compatibility history. Application deployment still appends the
  matching compatibility row to the migrated content database and fails closed if the two differ.
- The Git Legacy scripts and Project Manager runtime mirror must match byte-for-byte before a V2
  daily run can proceed.
- If today's Legacy issue is late, the launcher waits up to ten minutes and then may process the
  oldest unfinished exported issue in a bounded seven-day catch-up window.

## Supported Python launch model

Radar V2 deliberately remains a non-installable workspace. Approved module entrypoints require the
V2 root in `PYTHONPATH`; the daily launcher establishes this itself. The verification suite runs the
operator-facing modules from a foreign cwd with the same explicit environment contract.

## Rollback

The code rollback is the prior Git commit. Daily run evidence and failed attempts are retained and
must not be deleted. Existing pre-hardening combined reports remain readable; the shell summary
labels them `legacy_report_without_verdict` rather than rewriting immutable evidence.
