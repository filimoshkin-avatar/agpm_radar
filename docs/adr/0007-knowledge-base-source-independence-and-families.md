# ADR-0007: Source independence and source families

Date: 2026-08-22

Status: proposed — awaiting owner approval

## Context

Defect D13: KX has no model of source independence. Two rows in `documents` are two documents and
nothing in the schema says whether they are two pieces of evidence. Syndication, reprints and a
shared press release are invisible.

This matters more here than in an ordinary corpus. The radar's daily perimeter is news, and news
propagates by reprint: one vendor announcement becomes a dozen articles within a day. A rating that
counts "how many sources say this" and an idea gate that requires "at least two supporting claims"
both read repetition as corroboration. Without an independence model they read a press release's
distribution list as agreement among twelve observers.

Slice 1.1 measured the shape of the problem from the other side: of 8 313 documents, 8 308 come from
a single discovery registry that records where each URL was first seen, and the perimeter is 275
documents across 49 issues — small enough that the first families can be curated by hand rather than
inferred.

## Decision

### 1. The entities

| Entity | Holds |
|---|---|
| `source_families` | a group of outlets, domains or channels with a common owner, editorial desk or syndication channel |
| `document_source_family` | which family a document belongs to |
| `content_duplicate_clusters` | clusters of substantively duplicate content: reprint, syndication, shared press release, shared primary source |
| `duplicate_evidence` | why a cluster was formed: hash match, shingle match, or a shared cited primary source |

### 2. The counting rules

1. Two documents of the same `source_family` are **one** confirmation.
2. Documents of one `content_duplicate_cluster` are **one** confirmation, regardless of family. A
   reprint in an unrelated outlet is still the same text.
3. A press release and its reprints are one source. An analytical piece about the same release is a
   separate source **if it contains data of its own**; if it only restates, it is not.
4. **Independence is stored with the assessment, not computed on read.** A rating that changes
   because a family was edited afterwards is not a rating anybody can reason about. The independence
   verdict is written into the score, the idea and the gold-set expectation at the moment they are
   produced, together with the family and cluster versions it was based on.

### 3. Where the rules bind

5. Scoring (P22), the graph, admitting a candidate idea and the gold sets all apply them. They are
   not advisory.
6. A candidate idea needs **at least two supporting claims from different source families** (P13).
   Below that it is not shown to the owner.
7. Where a statement leans on repetition, the verifier checks independence as its third level
   (ADR-0004, §2.8).
8. The vertical slice measures "share of statements strengthened by documents of one family" against
   a threshold of **zero** (plan §13.1). This is not one of the numbers the owner calibrates after
   measurement — it is a defect count.

### 4. How families and clusters are formed

9. In the vertical slice (2.1) both are **hand-curated for 20-30 documents**, deliberately including
   a pair of known reprints of one primary source. The prototype exists to find out what the
   automatic rules must catch.
10. At scale (2.4), a cluster is proposed by evidence and each kind is recorded:
    - identical canonical text hash — certain;
    - shingle overlap above a threshold — probable, threshold recorded with the cluster;
    - the same cited primary source — a hint, not a cluster on its own.
11. A family is an editorial fact, not a computed one. It is proposed by domain and by observed
    syndication, and confirmed by a person. An append-only decision event records who confirmed it
    (ADR-0006, §3).
12. Absence of a family is not independence. A document with no family assignment is treated as
    **unknown**, and an unknown never satisfies a "two independent sources" requirement. Fail-closed:
    the default is not "presumed independent".

## Consequences

- Rule 12 makes independence expensive early: until families are assigned, almost nothing clears the
  two-source gate. That is correct — the alternative silently admits reprints — but it means family
  assignment is on the critical path of slice 2.9, not a later refinement.
- Storing the verdict with the assessment means a family correction does not retroactively change
  history. Re-scoring after a correction is a separate versioned event (P22), which is the intended
  behaviour and not a workaround.
- Shingle thresholds will misclassify. The threshold is stored with each cluster so a later review
  can tell which clusters were formed under which rule instead of guessing.
- The rules interact with the corpus-membership contract: independence is a property of documents,
  so it is computed inside a membership class and never across the union of classes. The canon is not
  a corroborating source for a claim about the news, and the news does not corroborate the canon.
