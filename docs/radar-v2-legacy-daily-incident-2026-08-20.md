# Radar V2 — Legacy daily incident compatibility, 2026-08-20

Status: V2 candidate boundary updated and regression-tested. Legacy repair remains an independent
working-tree change owned by Project Manager.

## Incident and corrected Legacy semantics

The canonical 2026-08-20 collection completed, but its first report contained zero included
materials. The low-yield Perplexity expansion used the technical run id
`2026-08-20-perplexity-expansion` and replaced the canonical `state.last_run_id`. The external
publication guard expected exactly `2026-08-20`, so it stopped delivery and site publication even
though the report artifacts already existed. No source data was lost.

Project Manager repaired Legacy by separating the canonical daily identity from auxiliary-run
identity, recording issue readiness explicitly, and allowing a successfully built empty issue.
The report selection rule was also expanded: a material first considered for the current issue may
be included when it has not appeared in an earlier issue and either its verified/low-confidence
publication date is within the preceding 30 calendar days or its publication date is unresolved.
The rebuilt 2026-08-20 issue included 10 materials and passed the production health check.

## V2 invariant

Radar V2 now enforces the selection rule at the daily candidate-to-mutation boundary, before any
staging database or immutable publication package can be accepted:

- an included material id must not already be linked to any earlier issue state;
- `resolved` and `low_confidence` materials require `publishedAt` between issue date minus 30 days
  and the issue date, inclusively;
- an `unresolved` material is accepted only with `publishedAt=null`;
- a future date, a date older than 30 days, or a repeated material fails closed;
- historical correction candidates are unaffected and retain their separate optimistic controls.

This protects V2 even if a future Project Manager adapter emits an over-broad daily candidate.
Stage 13 publisher orchestration must additionally keep canonical issue identity separate from
auxiliary collection attempt identity and gate publication on the immutable candidate/package
state rather than on the last technical collection run id.

## GRACE and verification

Radar has no `M-*`/`V-M-*` module map, so the GRACE module delta is explicitly skipped. Governed
scope is limited to the Stage 5 daily candidate boundary, its regression, and this evidence note.
The existing uncommitted Legacy pipeline repair was inspected but not modified or included.
