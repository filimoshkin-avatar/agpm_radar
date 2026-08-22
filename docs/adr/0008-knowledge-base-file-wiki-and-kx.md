# ADR-0008: The file wiki and KX — roles, direction of synchronization, source of truth

Date: 2026-08-22

Status: accepted 2026-08-22 by the owner

## Context

Owner decision P24 settled what the knowledge base is: not a new wiki, but a **published projection
of the existing AgPM wiki, backed by evidence from KX**. The wiki exists, is written by hand by
Project Manager, and follows written rules in `agpm/SCHEMA.md`.

Two stores, two natures. `knowledge/` is markdown on the control host, authored, not under version
control. KX is PostgreSQL on Local Ru, immutable, with exact character offsets and provenance. They
will diverge — P28 accepts that and requires the divergence to be visible.

Slice 1.5 measured the wiki instead of assuming it, and three of the measurements change what this
ADR has to say:

- the authored surface is **63 pages**, not the 93 files the plan counts (32 are immutable raw
  extracts, 3 are bookkeeping);
- **27 of 63 pages cite nothing at all**, `agpm/wiki/overview/agpm-overview.md` among them — the
  compiled model page the taxonomy is supposed to rest on;
- the `SCHEMA.md` relationship vocabulary is defined but **effectively unused**: 148 internal links
  exist and all are untyped.

Slice 1.1 measured the other side: the file store already holds five extracted texts that KX has no
complete version of, and 136 registry rows that never reached KX.

## Decision

### 1. Roles

| | `knowledge/` (files, control host, written by Project Manager) | KX (PostgreSQL, Local Ru) |
|---|---|---|
| Source of truth for | the compiled AgPM model, authored synthesis pages, editorial decisions | full text, immutable versions, exact spans, provenance |
| Unit | a markdown page | document, version, chunk, span |
| Immutability | `raw/` by convention | enforced by database triggers |
| Versioning | none — the directory is not under git | versions and hashes in the schema |
| Role in the product | what we assert | what we prove it with |

### 2. Source of truth

1. **On any divergence about evidence, KX is the truth. Always.** If a page quotes a source and KX's
   stored text does not contain that quotation at that span, the page is wrong — not the store. There
   is no case in which an authored page overrides an immutable span.
2. On any divergence about **what we assert**, the wiki is the truth. KX holds no opinions.
3. A statement with no evidence is not thereby false. It is unsupported, it is counted, and it is not
   published as evidence-backed. 27 pages are in that state today and that is a work item, not a
   defect in the wiki: `SCHEMA.md` makes Supporting sources conditional, and the pages were written
   as a compilation.

### 3. Direction of synchronization

4. Synchronization is **one-way: KX imports from the file contour, never the reverse.** Nothing
   writes into `knowledge/` from KX.
5. Existing pages are **never rewritten automatically** (plan §16). The machine proposes additions
   and evidence bindings; editing authored text is the owner's decision.
6. **The target direction is that the file contour reads texts from KX through the research API**,
   and its own collection does not grow. `agpm-radar/data/source-fulltext/` is a working copy that
   should shrink over time, not a second evidence base that should be kept in step.
7. The existing loop `runs/` → `wiki/daily/` → `wiki/monthly/` → the main AgPM wiki is a working
   self-population mechanism. The designed contour **joins it and does not replace it**.

### 4. Reconciliation is mandatory (P28)

8. A **regular reconciliation report** compares the file store and KX: what exists in only one, where
   the texts differ, which version is newer. Recorded in `store_reconciliation_reports`
   (migration 003), immutable.
9. **Coverage is not reported without it.** A coverage figure computed while the two stores disagree
   is a number about one store presented as a number about the system.
10. The first run already found what the report is for: five cached full texts with no complete
    version in KX, and 136 discovery rows added since the corpus snapshot KX imported. None is an
    error on its own; learning about them at publication time would be.

### 5. What is imported from the wiki, and how

11. Pages become `concepts` / `concept_versions`; the file path is part of the concept's identity.
12. The section structure is taken as it is, with an **optional** mapping onto the six `SCHEMA.md`
    conventions (ADR-0006, §21). Headings are matched **bilingually** — the wiki writes both
    `## Purpose` and `## Назначение`, and treating only the English form as canonical would halve the
    measured conformance for no reason.
13. Atomic claims are the list items under a claim-bearing section: 332 candidates across 63 pages.
    The work splits in two and is planned as two: **34 pages parse mechanically**, and **29 pages
    need prose segmentation** by a model with human confirmation. The second is where "the machine
    rewrote the author's text" would happen, so it proposes bindings and never edits.
14. The relationship vocabulary — `supports`, `extends`, `constrains`, `contradicts`,
    `operationalizes`, `depends-on` — is carried into the graph schema unchanged (P24). But
    **`concept_links` cannot be imported**: no typed links exist in the files. The 148 untyped links
    give the graph its skeleton; every edge type has to be authored. "The graph is already in the
    files" is false and planning must not assume it.
15. Five sections of the model — `data/`, `market/`, `maturity/`, `open-questions/`, `risks/` — are
    **empty directories**. Slice 2.5б will find five gaps there, not refinements to existing text.

### 6. The canon closes the evidence gap

16. Pages about the White Paper, the Manifesto and the standards cannot be supported by radar
    material. The canon is loaded into KX as its own membership class (P25, slice 1.6) so those
    pages have something to point at.
17. **Not every canon file is the text of its source.** Of 32 files, 24 are faithful conversions,
    5 are excerpts written by us and 3 are our notes. Only a faithful conversion may back a
    quotation attributed to the original; the other eight are imported with provenance that blocks
    public quotation. They remain usable for extraction and for research answers.

## Consequences

- The wiki keeps being written exactly as it is written today. Nothing is asked of Project Manager.
- Rule 1 means an authored page can be proven wrong by the store, and there has to be a place for
  that finding to land: the "statements without evidence" report of slice 2.5 also reports statements
  whose evidence contradicts them.
- The reconciliation report will keep finding divergence, permanently. That is success, not a backlog
  to drive to zero: the two stores have different jobs and different update rhythms.
- Because `concept_links` must be authored, the graph is a slower deliverable than the plan's
  "carry the vocabulary across" implies. Better known now than at slice 2.11.
- The one-way rule means the file contour's own full-text collection is a temporary structure. It is
  not deleted by this ADR; it stops being extended.
