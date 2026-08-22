# ADR-0004: Evidence, claim binding and abstention

Date: 2026-08-22

Status: proposed — awaiting owner approval

## Context

The Radar knowledge base publishes statements about other people's material. Owner decision P2 is
that only quotations and our own writing are published, never a reprint; P19 is that the structural
layer — quotations, figures, translations — publishes automatically once fail-closed conditions
hold. Both make the evidence contract load-bearing: nothing downstream can be safer than the rule
that decides what counts as evidence.

KX already enforces the narrow part. `claim_evidence` carries a trigger that accepts a row only when
the quoted substring matches the stored canonical text exactly and its SHA-256 agrees, and
`canonicalize_text` normalizes to NFC so PostgreSQL and Python agree on character offsets.

What is missing is the wide part. A verifier that checks numbers, dates, quotation marks and links
will pass a qualitative sentence that contains none of them and is supported by nothing. That is the
failure mode a knowledge base has to be built against, because it is the one a reader cannot detect.

This ADR fixes the evidence unit, what binding a statement to evidence means, and what happens when
there is no basis. It does not specify the extraction pipeline (slice 2.6), the translation pipeline
(2.8) or the public agent (3.4).

## Decision

### 1. The unit of evidence

1. A piece of evidence is `(version_id, char_start, char_end, quote_sha256)`. Nothing coarser is
   evidence: a document reference is a citation, not a span, and cannot be verified.
2. `claim_evidence` holds **exact spans only**. The database trigger stays as it is and gains no
   `candidate` state.
3. An extraction that did not align to a span lives in `extraction_candidates` with the reason it
   did not align. It never reaches the fact layer, the graph or an answer. Promotion to
   `claim_evidence` happens only through an explicit validation that produces an exact span.
4. A model is never the source of an exact value. Numbers, dates, units, currencies, verbatim
   quotations and links come from KX rows; they are parsed deterministically out of a span that has
   already been verified.
5. Every answer run is reproducible from what was stored, or it is rejected. A run that cannot be
   replayed is not evidence of anything.

### 2. Claim binding

6. In strict mode **every factual clause of a generated text must reference an accepted `claim_id`
   and the exact evidence of that claim.**
7. Free model text with no claim binding is an error. Connective phrasing that carries no factual
   content is allowed and is marked as non-substantive; everything else unbound is a defect, not a
   style issue.
8. The verifier checks three levels, and all three must pass:
   - every factual clause is bound to an accepted claim;
   - the tokens of the clause — numbers, dates, units, names, quoted text, links — agree with the
     cited span;
   - source independence holds wherever the statement leans on repetition (ADR-0007).

### 3. Abstention

9. When there is no basis, the answer is a structural refusal, not a hedged sentence. "Probably",
   "it appears that" and "sources suggest" are ways of publishing an unsupported claim while sounding
   careful, and they are forbidden in strict mode.
10. **A refusal carries an internal reason code**, not only a sentence:
    - `no_evidence` — the fact is not in the evidence base at all;
    - `out_of_scope` — the fact is in the evidence base but not reachable from the asker's scope.
    The outward wording is a policy of the scope; the internal code is always precise. The two are
    separated now rather than later because refusal semantics harden into the gold sets, and changing
    them afterwards means changing the gold sets with them.
11. Every element of an evidence package carries an `audience` label. In the first version it is the
    constant `public`. Without the field the renderer has no way to decline to quote something the
    asker may not see, and the check has to be added under pressure later.

### 4. Automatic publication of the structural layer (P19)

12. A quotation, a figure or a translation publishes with no human and no batch approval when **all**
    of the following hold. Any one of them failing sends the element to `publication_quarantine`
    with the reason, and never out with a caveat:
    1. the original quotation matches its immutable span exactly;
    2. coordinates, hash, URL and provenance are valid;
    3. figures, dates, units and proper names pass their deterministic checks;
    4. a translation is shown together with the available original and marked as machine-produced;
    5. source independence holds where it applies.
13. Authored wiki text and the wording of insights are outside this rule and stay under owner
    approval (P4).

### 5. Translations

14. A translation is a stored, versioned entity attached to the span of the original, with its
    author (model or person), prompt hash and verification state.
15. The original is always available. Without a stored original, a translation is not published.
16. Numbers, dates and units are invariant character by character across a translation; a divergence
    is a blocking error.
17. **Proper names are checked through the alias table, not by string equality.** "John Smith" and
    "Джон Смит" cannot be compared as strings across scripts. A name in a translation is accepted
    when it equals the original or is a registered alias of the same entity in `entity_aliases`.
18. **An unregistered spelling does not block the quotation (P36).** The quotation publishes, the
    name is shown in the original script, and a proposed alias goes to a queue with no deadline.
    Latin script inside Russian text is not an error and needs no "unverified" marker. An alias
    decision is an append-only event with an actor.
19. A translation never stands in for evidence. Fact checking runs against the original span.

### 6. Publication and links

20. A public quotation is at most a paragraph, with attribution and a link to the source (P32). One
    rule for every kind of source, standards included (P34). The limit is enforced by the automatic
    publication check, not by editorial care.
21. **A quotation and its link point at the version the quotation came from.** Text taken from a web
    archive links to the archive snapshot and its capture date. Migration 003 makes this computable:
    `version_publication_block` refuses any version whose provenance is missing, flagged for review,
    or claims an archive with no snapshot identity. Four documents are in that state today.
22. How a document was obtained does not affect whether it may be quoted (P11). Restrictions come
    from access classes (ADR-0005), never from the type of the source.

## Consequences

- The verifier is more expensive than a token checker and is the only thing standing between the
  product and confident unsupported prose. That cost is accepted.
- Strict mode will refuse questions a reader expects an answer to. The refusal rate is measured on
  the vertical slice and the owner sets the bar before scaling (P29); zero-tolerance thresholds —
  numbers, translations, independence, abstention, injection, authorization — are not negotiable and
  are not part of that measurement.
- `extraction_candidates` will be large and mostly never promoted. That is the point: it is where
  unverified extraction is visible instead of invisible.
- The four archive-sourced documents cannot be quoted publicly until their snapshots are recovered
  (slice 2.3). Accepted as a temporary limitation, and it is computed rather than remembered.
- Reason codes and `audience` are dead weight in the first version. They cost a field each now and a
  gold-set migration later.
