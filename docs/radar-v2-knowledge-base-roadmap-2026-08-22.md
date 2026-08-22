# Radar V2 Knowledge Base Roadmap

Date: 2026-08-22

Status: owner direction recorded; planning source of truth for post-migration Radar V2 development.

## START_CHANGE_SUMMARY

- Goal: replace the obsolete migration-next-stage framing with a product-development roadmap for
  Radar V2 as the production system on Local Ru, focused on a self-populating evidence-grounded
  knowledge base.
- Source of truth: this roadmap, `docs/radar-kx-production-fulltext-2026-08-21.md`,
  `docs/radar-kx-issue-perimeter-2026-08-21.md`, and
  `/root/.openclaw/workspace/reports/radar-kx-fulltext-close-2026-08-22.md`.
- Affected docs only: no production service, database, Caddy, DNS, firewall, cron, or runtime file
  is changed by this note.
- GRACE-Delta: skip - Radar has no `M-*`/`V-M-*` module map. This is a planning document, not an
  implementation change.

## Owner Direction

The previous 19-stage migration plan is complete. Radar now has:

- independent Legacy production at `https://radar.aipractice.space`;
- production Radar V2 on Local Ru at `https://radar.agpm.space`;
- owner observation over both systems;
- active development focus on V2 only.

Future work must not be framed as Stage 17-19, cutover, or Legacy retirement by default. Those were
migration concerns. The new product direction is Radar V2 plus an additional knowledge-base layer
built from Radar materials.

## Current Baseline

Radar KX is the first production step toward that knowledge base:

- KX is a separate loopback-only PostgreSQL evidence store on Local Ru.
- It preserves raw bodies, canonical full text, chunks, provenance, exact hashes, and immutable
  document versions.
- As of the 2026-08-22 closeout, the active Radar V2 issue perimeter contains 77 issues, 277
  selected-material rows and 275 unique selected documents.
- KX now has complete full text for 275/275 selected documents in that perimeter, missing `0`.
- The 25-document closeout was a production hotfix path using `operator_artifact`; it must be
  normalized into schema migration, CLI import flow, tests and release evidence before it becomes
  routine.

## Target Use Cases

1. Build a separate knowledge base from collected Radar articles with reliable search.
2. Build knowledge graphs, visualization, idea ratings and insight ratings.
3. Support LLM-assisted research over the corpus with exact numbers, quotes, facts and source
   links for every factual answer.

## Technical Stack Hypothesis And Open Decisions

The stack below is the current hypothesis from previous joint research, not a mandatory
architecture. It should be used as a concrete starting point for comparison, then revisited against
target use cases, data quality, operational constraints, resource budgets, implementation risk and
acceptance criteria:

`Trafilatura/Docling -> immutable versions -> LangExtract behind an adapter -> PostgreSQL FTS +
pgvector -> custom evidence-grounded answer/verifier -> SQL graph + Cytoscape.js`.

The next reasonable step is not implementation. It is to discuss the main forks, rethink both
functional and technical architecture, and then write the durable design package:

- ADR for evidence/answer architecture and unsupported-claim behavior;
- schema v0 for extraction jobs, claims, entities, evidence spans, graph edges and idea scores;
- API/evidence contracts for retrieval, answer packets and verifier output;
- resource budget for fetch, parse, extraction, embeddings, storage, latency and model cost;
- exact acceptance plan and gold datasets before production migration or public UI/API exposure.

Important forks to discuss before locking the plan:

- document extraction: Trafilatura, Docling, source-specific parsers, browser-rendered extraction,
  or hybrid routing;
- versioning and immutability granularity: raw blobs, canonical text, chunks, extraction outputs and
  review decisions;
- extraction engine: LangExtract adapter, direct structured LLM calls, deterministic NLP, or a
  mixed pipeline;
- retrieval: PostgreSQL FTS only, pgvector only, RRF fusion, reranker, or staged rollout;
- answer architecture: strict evidence packets, LLM tool-calling, deterministic renderer/verifier,
  and unsupported-claim behavior;
- graph layer: pure SQL graph, graph database, materialized graph exports, Cytoscape.js or another
  bounded visualization surface;
- deployment boundary: internal CLI/report first, internal API/UI, or later public surface.

## Product Principles

- KX source evidence is immutable; embeddings, extracted claims, entities, graph edges, rankings
  and answer packets are derived and rebuildable.
- Full-text collection must be self-populating for future Radar materials, with explicit queue
  states, provenance, source-specific parsers and operator-artifact fallback.
- The answer layer must be evidence-grounded: LLMs may select and structure findings, but exact
  quotes, numbers, dates, units and links must come from deterministic KX evidence spans.
- Unsupported claims must fail closed in strict mode as `insufficient_data`.
- Robots, paywalls, authentication and access-control bypasses require a separate owner policy
  decision. Do not silently normalize bypasses into the pipeline.
- Start with internal/operator surfaces before public API or public UI exposure.

## Development Tracks

### Track A. KX Ingestion And Corpus Growth

Goal: make full-text collection routine and self-populating for every new Radar V2 material.

Deliverables:

- schema migration and release commit for `operator_artifact` source kind;
- CLI import path for operator-provided artifacts with provenance, hashes and verifier coverage;
- daily or post-publish perimeter sync from Radar V2 into KX;
- retry/failure dashboard or report for new gaps;
- source-specific fetch/parser playbooks for common blockers, without policy violations.

Acceptance gates:

- KX tests, strict type/lint gates and full verifier pass;
- restored backup verifies after schema changes;
- a new Radar issue can enter KX automatically and report complete/missing state;
- manual artifact import is reproducible, audited and not a one-off SQL hotfix.

### Track B. Search And Retrieval

Goal: make the full-text corpus searchable before adding heavier intelligence.

Deliverables:

- PostgreSQL lexical search over chunks/documents with Russian and English support;
- search API or internal CLI returning document IDs, snippets, offsets and source URLs;
- gold question set of 50-100 mixed Russian/English queries;
- pgvector multilingual embeddings and RRF fusion after lexical baseline is measured;
- reranking only after measured quality improvement and cost/latency budget are explicit.

Acceptance gates:

- Recall@10 and latency targets defined and measured;
- every result can be traced to document version and chunk offsets;
- no answer generation is allowed to cite a source absent from retrieval evidence.

### Track C. Extraction, Claims And Knowledge Graph

Goal: turn articles into structured, reviewable knowledge rather than unverified summaries.

Deliverables:

- extraction job tables for model, prompt/config, document version, status, cost and errors;
- claim candidates with exact-span evidence requirements;
- entities and reversible entity-resolution decisions;
- graph edges tied to `claim_id -> evidence_id`, with source document and quote provenance;
- bounded graph export suitable for Cytoscape.js or equivalent visualization.

Acceptance gates:

- fuzzy/null-span claims cannot enter factual graph or strict answers;
- manual precision sample is recorded;
- graph nodes/edges are rebuildable from stored extraction runs and reviewed decisions.

### Track D. Evidence-Grounded LLM Answers

Goal: allow research questions over Radar materials with exact citations and no unsupported prose.

Deliverables:

- answer contract: question, retrieval set, evidence packet, structured LLM output, deterministic
  renderer and verifier;
- strict mode with `insufficient_data`;
- quote/number/date/unit verifier against KX spans;
- source-link renderer for every fact;
- audit log of prompts, models, evidence IDs and final answers.

Acceptance gates:

- zero unsupported claims on a gold QA set;
- zero numeric/quote drift in strict mode;
- answer can be reproduced or rejected from stored evidence without trusting LLM prose.

### Track E. Idea And Insight Ratings

Goal: rank ideas and insights extracted from the corpus in a way that remains explainable.

Deliverables:

- idea/insight candidate model;
- versioned scoring dimensions, for example novelty, strategic relevance, evidence strength,
  recurrence, contradiction, time sensitivity and actionability;
- explanation packets pointing to source claims and quotes;
- review workflow for promoted ideas;
- trends over issues and time.

Acceptance gates:

- every rating explains which evidence affected the score;
- scoring versions are immutable and comparable;
- promoted ideas can be traced back to article quotes and issue context.

### Track F. Operator UI

Goal: provide usable internal surfaces before public exposure.

Deliverables:

- internal search/evidence viewer;
- document detail with versions, chunks, fetch attempts and provenance;
- gap queue and operator-artifact import status;
- graph viewer over bounded server-selected subgraphs;
- idea/insight ranking screen with explanation drill-down.

Acceptance gates:

- no private/internal raw table exposure;
- UI links always resolve to evidence-backed records;
- large graphs are bounded server-side and cannot overload the browser.

## Recommended First Slice

The first safe engineering slice is Track A:

1. Write a design note for normalized `operator_artifact` import.
2. Add KX schema migration and source-kind constraints for the 2026-08-22 hotfix behavior.
3. Implement a CLI import command that ingests HTML/text artifacts into complete document versions
   with provenance, hashes and queue/best-version updates.
4. Add regression tests and full verifier coverage.
5. Produce a release candidate, then request explicit approval before any production migration.

This slice turns the emergency full-text closeout into maintainable product infrastructure without
changing public Radar behavior.

## Explicit Non-Goals For The Next Planning Pass

- Do not plan Legacy retirement as the default next step.
- Do not plan DNS cutover as the default next step.
- Do not expose KX publicly before internal evidence/search contracts are accepted.
- Do not delete retained Legacy, V2, KX, backup, failed-attempt or migration artifacts.
- Do not bypass robots, paywalls, authentication or source access controls without a separate
  written policy decision.
