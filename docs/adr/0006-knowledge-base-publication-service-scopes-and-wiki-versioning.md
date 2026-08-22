# ADR-0006: Publication into a PostgreSQL schema, the KB service, scopes and wiki versioning

Date: 2026-08-22

Status: proposed — awaiting owner approval

## Context

The knowledge base publishes an immutable release: what the wiki says, backed by evidence, as of a
moment. Owner decision P35 chose the form — a separate PostgreSQL schema plus its own read-only KB
service — over an SQLite file inside the Radar V2 API (which would have coupled two products'
release cycles) and over a static export (which cannot filter by viewer at all).

Two decisions constrain how the release refers to the wiki. P27: the wiki is not under git and will
not be; its state is pinned by a snapshot at each publication. P37: the history between publications
is closed by a structured edit journal rather than by introducing git.

One risk is worth naming before it hardens. Scope today means "which evidence base" and there are
exactly two — the published release and the full KX. That is the right answer to the current
question and the wrong shape for a third level of access, which is not a third column but a filtered
view. Generalizing costs a sentence now and a migration later, so it is done now.

Slice 1.5 measured the wiki this release will project: 63 authored pages, 38 480 words, 4.1 MB of
changing text under `agpm/**` and `agpm-radar/wiki/**`.

## Decision

### 1. The published release

1. A `knowledge_release` is an **immutable slice**: its composition, counters, a hash of its state
   and the moment it was published. The active pointer switches atomically and can be rolled back.
2. The release lives in its **own PostgreSQL schema**, served by its **own read-only KB service**
   with its own release cycle and its own blast radius (P35).
3. **The slice file is not a public artifact.** Every read goes through the service. A static file
   cannot filter by viewer, and publishing one would make the "opened = visible to everyone"
   assumption structural instead of merely current.
4. **Every element of the slice carries `audience`.** In the first version it is the constant
   `public`, and the service checks it on the way out **from day one**. A check added later is a
   check that was missing in between.
5. The public issues API of Radar V2 stays about issues. It is not extended into knowledge.

### 2. Scopes and authorization

6. **A scope is a named row of an extensible registry, not one of two literals.** A role is a set of
   scopes. An endpoint checks a **capability** — "sees drafts", "sees full text", "publishes" — never
   a role name and never `if editor`. There are exactly two scopes today; that is the state of the
   registry, not a property of the model.
7. Authorization is **server-side on every privileged endpoint**. Not a hidden button, not a client
   check, not an inference from the route.
8. The scope travels with the request; the endpoint decides. A session default is not a decision.
9. Minimum two roles (P31): an **editor** sees drafts and publishes; a **researcher** asks questions
   against the full KX but does not publish and does not see the approval queue. Invitation, revocation
   and role change are privileged actions with an audit record.
10. **The answer cache key is `(normalized question, scope, release_id)`.** A cache without scope in
    the key moves content between access levels, and it does so silently.
11. Object access is checked by owner and scope, never by knowledge of an identifier (IDOR). CSRF
    protection covers every mutating editor operation. Limits are separate for public and editor.
12. Privileged actions are audited: who, what, when, with which scope, on which object.
13. The editor mode lives on the public domain behind a login, for example `radar.agpm.space/editor`
    (P21). The same product interface; the difference is server-side.
14. These leaks must be closed by **negative tests** with a zero threshold, not by care: a draft
    through a public endpoint; full text through a public endpoint; another scope's object by direct
    identifier; scope escalation by parameter substitution; a public answer citing evidence from an
    unpublished document.

### 3. Editorial decisions are events

15. **Every editorial decision is an append-only event with an actor**, not a status overwritten in
    place. Approval, rejection, re-scoring, an alias decision, a publication policy change: each is a
    row that says who decided and when. A status column that is updated cannot answer "who decided
    this, and when did it change".

### 4. Wiki versioning

16. Before assembling each release, the state of the file wiki is **snapshotted into KX** and the
    release points at that snapshot (P27). Git is not introduced.
17. The snapshot is a **manifest of per-file SHA-256 plus content-addressed blobs**, the idiom
    `raw_blobs` already uses. A whole-directory copy is not acceptable: deduplication makes an
    unchanged page free, per-page hashes make the difference between two releases computable, and a
    later move to git becomes an import script.
18. **The snapshot perimeter is `agpm/**` and `agpm-radar/wiki/**`.** `agpm-radar/data/`, `reports/`
    and `runs/` are the radar's operational artefacts, not the state of the wiki, and they are 240 MB
    that must not enter a release. Measured: the perimeter is 40.7 MB, of which 36.6 MB is the
    immutable `raw/originals/` stored once, leaving 4.1 MB that actually changes.
19. **`wiki_edit_journal` records every page mutation** (P37): the page, the hash before and after,
    who initiated it and why. Immutable discipline is applied to other people's text; without this
    journal none is applied to our own.
20. Accepted price, stated plainly: between publications, the snapshot keeps only the final state.
    The journal answers "who and why"; the snapshot does not reconstruct intermediate text.
21. `concept_versions` stores an **ordered list of sections, each with an optional canonical role**
    among the six `SCHEMA.md` conventions — not six fixed columns. Slice 1.5 measured why: 3 of 63
    pages carry all six, and 257 of 297 distinct level-two headings map to none of them. Sections
    that map take part in the projection and in the split into claims; the rest are stored unchanged.
    Forcing pages into six sections would mean automatically rewriting authored text, which is
    forbidden.

## Consequences

- One more deployable component with its own release cycle. Its cost was already in the plan as an
  unwritten KB API; P35 makes it explicit rather than adding it.
- The capability check is more code than `if editor` at every endpoint and is the reason a third
  access level will be a filter rather than a rewrite.
- `audience` is a constant column for the whole first version. Accepted.
- Snapshots are cheap only because the perimeter is chosen. A future contributor who widens it to
  "the whole knowledge directory" reintroduces the storage problem this decision removed, so the
  perimeter belongs in the code that takes the snapshot, not in a runbook.
- Append-only editorial events mean the review queue is a projection over events, not a table of
  mutable rows. Slightly more work to read; the only way "who decided" survives.
