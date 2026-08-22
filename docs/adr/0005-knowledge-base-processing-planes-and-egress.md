# ADR-0005: Processing planes, the egress contract and the KX access invariant

Date: 2026-08-22

Status: accepted 2026-08-22 by the owner

## Context

Radar KX holds the full text of other people's material. Until 2026-08-18 the argument for keeping
processing on Local Ru was that the text must never leave it. Owner decision P18 retired that
argument: full documents and the fragments a task needs may be sent to the two approved model
endpoints. Permanent storage stays on Local Ru; controlled egress for processing is allowed.

That changes what the network policy has to protect. It is no longer "no text leaves"; it is "text
leaves only to two named endpoints, only as much as the task needs, and every call is recorded".
P30 removes the precondition that a provider guarantee no retention or training — the work is not
blocked on it, and the audit exists for us regardless.

Two facts about the host shape the rest. Hermes 0.20.0 runs on Local Ru with exactly two models,
`zai/glm-5.2` and `minimax/MiniMax-M3` (P9), and the same host already runs two NRD profiles that
demonstrate the isolation pattern. Defect D7 records that the ingest unit's hardening
(`MemoryDenyWriteExecute=true`) is incompatible with a JIT, so browser rendering cannot live inside
it.

## Decision

### 1. Where each kind of work runs

| Work | Plane | Why there |
|---|---|---|
| Document extraction and parsing | Local Ru — Hermes profile plus an orchestrator | locality to the database, one place of permanent storage, the approved model gateway. **Not** "the text must not travel" — that reason is retired |
| Authored rewriting | control host | it works from what was extracted; it does not need full text |
| Material scoring (P22) | control host, Project Manager | level one reads the published release, level two goes through the authenticated research API |
| Public and editor Q&A | Local Ru, Hermes profiles | they read the release or KX according to scope |

### 2. The egress contract (P18)

1. **Only the approved GLM and MiniMax endpoints.** Everything else is denied by default.
2. The restriction is enforced by the systemd unit of the Hermes profile, not only by application
   configuration. An application-level allowlist is a setting; a unit-level one is a property.
3. **Context minimization.** A run sends the smallest fragment that does the job, not the whole
   document when the job does not need it. The rule is fixed per run type and recorded with it.
4. **Every call is audited** in `egress_audit` (migration 003): what was sent, how large, to which
   provider and model, under which processing run. The table is immutable.
5. Retention and training are refused wherever the API or the provider's terms support refusing them.
   Actual support is checked and recorded, never assumed. Absence of support does not block the work
   (P30).
6. **The orchestrator has no internet.** Loopback to Hermes and the PostgreSQL unix socket, nothing
   else.

### 3. Hermes profiles, not agents inside the shared gateway

7. Knowledge-base work runs in **its own Hermes profiles**, never in the shared gateway. A profile is
   effectively a separate instance: its own `HERMES_HOME`, configuration, state, keys, port and unit
   from the `hermes-gateway@.service` template. Code and virtualenv are shared.
8. Separate profiles are required for network policy, privileges (the shared gateway runs as root),
   failure isolation, state isolation and independent lifecycle. NRD's `nrd-intake` and
   `nrd-analysis` on this host are the precedent.
9. Three NRD findings are reused rather than rediscovered:
   - a profile needs **its own runtime**; a shared virtualenv resolves through `/root` and dies under
     `ProtectHome`;
   - the **model allowlist patch** is required: Hermes accepts an arbitrary model identifier even
     with `model_routes` configured, so the two-model limit (P9) is not enforced without it;
   - environment files are **root-owned, mode 0600, one per profile**.
10. The contours on Local Ru are: the shared gateway (exists, untouched), the extraction profile, the
    public agent profile, the editor Q&A profile.

### 4. Browser rendering is its own unit

11. Rendering runs in a **separate unit** without `MemoryDenyWriteExecute`, under its own user and
    with its own network policy (defect D7). It is rung five of the acquisition ladder. It is built
    when the gap queue shows documents that only a browser can obtain — by measured need, not
    speculatively.

### 5. The interface boundary

12. The reader talks to the Radar interface. Hermes is not visible, has no public address and is not
    the author of an answer.
13. Search, evidence-package assembly, limits, deterministic answer assembly and verification live in
    Radar code. The model returns structure and references.
14. A Hermes outage does not break the knowledge base: the wiki, search, the graph and the ratings
    read from the release and work with no model at all.
15. **A reader's question is data, not an instruction.** So is the content of someone else's article
    inside an evidence package. Output that follows an instruction found in either is rejected. Both
    are closed by negative tests with a zero threshold (plan §13.1), not by prompt wording.

### 6. The KX access invariant

16. **KX has no public access**: no public port, no Caddy route, no DNS record. Full text is reached
    only through the authenticated editor/research API with a server-side scope check.
17. The invariant is stated as *no public access*, deliberately not as *one database on one host*.
    An internal read replica for research load is not forbidden by this ADR; it would be a topology
    decision taken on measured need, and it does not weaken the invariant as written.
18. KX is never in the publication path of Radar V2 issues. The only coupling is one-way and
    read-only: V2 exports its perimeter, KX imports it.

## Consequences

- The knowledge base gains three systemd units on Local Ru, each needing an owner-approved change.
  None is created by this ADR.
- The model allowlist patch is a hard prerequisite: without it, P9's two-model limit is a convention
  rather than a control.
- `egress_audit` grows with every model call and is immutable. Storage cost is accepted; it is small
  next to the raw blobs.
- Context minimization is a per-run-type rule, so a new run type is not finished until its rule is
  written down.
- Keeping the invariant as "no public access" leaves a read replica available later without amending
  this ADR. The cost is that "KX is one database" cannot be relied on as an invariant elsewhere.
