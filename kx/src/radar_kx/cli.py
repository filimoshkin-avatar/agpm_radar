from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
from typing import Any

from radar_kx.acquisition import LADDER, HostProfile
from radar_kx.artifact_import import (
    import_artifact,
    load_artifact_manifest,
    load_provenance_corrections,
    record_provenance_corrections,
)
from radar_kx.cache_import import import_caches
from radar_kx.canon_corpus import canon_summary, import_canon, scan_canon
from radar_kx.config import Settings
from radar_kx.database import SCAN_SCOPES, Database
from radar_kx.dates import summarize as summarize_dates
from radar_kx.duplicates import (
    DEFAULT_SHINGLE_THRESHOLD,
    DEFAULT_SHINGLE_WIDTH,
    find_hash_clusters,
    find_shingle_clusters,
)
from radar_kx.editor_service import generate_token, serve
from radar_kx.evaluation import evaluate, load_gold_set
from radar_kx.extraction import ExtractionError, ProposedClaim, align_all, prompt_sha256
from radar_kx.graph import unsupported
from radar_kx.ideas import (
    DEFAULT_OVERLAP,
    build_idea_prompt,
    idea_prompt_sha256,
    parse_idea,
    summarize,
)
from radar_kx.identifiers import sha256_bytes
from radar_kx.issue_perimeter import load_perimeter_export
from radar_kx.manifest import load_manifest
from radar_kx.orchestrator import (
    ALLOWED_MODELS,
    IDEA_STATEMENT,
    QUOTE_TRANSLATION,
    RESEARCH_ANSWER,
    RUN_TYPES,
    TOPIC_ASSIGNMENT,
    HermesExtractor,
    ModelGateway,
    OrchestratorError,
)
from radar_kx.publication import (
    build_translation_prompt,
    check_invariants,
    parse_translation,
)
from radar_kx.reconciliation import load_inventory
from radar_kx.research import (
    answer_prompt_sha256,
    build_answer_prompt,
    refuse,
    render,
    verify,
)
from radar_kx.research import parse_answer as parse_research_answer
from radar_kx.search import MATCH_MODES, SCOPES
from radar_kx.skeleton import load_authored_skeleton
from radar_kx.source_families import (
    batch_payload,
    load_family_batch,
    propose_families,
)
from radar_kx.spans import summarize as summarize_spans
from radar_kx.topics import (
    build_payload,
    build_rubricator,
    parse_assignment,
)
from radar_kx.vertical_slice import load_candidates
from radar_kx.vertical_slice import select as select_slice
from radar_kx.wiki_snapshot import read_bundle
from radar_kx.worker import run_until_idle


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar-kx")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_manifest_parser = subparsers.add_parser("import-manifest")
    import_manifest_parser.add_argument("path", type=Path)
    import_manifest_parser.add_argument("--source-name", default="materials.jsonl")

    # Rung seven of the acquisition ladder: material that arrived as a file.
    import_artifact_parser = subparsers.add_parser("import-artifact")
    import_artifact_parser.add_argument("--manifest", type=Path, required=True)

    # Load the AgPM canon and the external standards as their own corpus.
    import_canon_parser = subparsers.add_parser("import-canon")
    import_canon_parser.add_argument("--raw-dir", type=Path, required=True)
    import_canon_parser.add_argument(
        "--originals-dir",
        type=Path,
        help="where the Word and PDF originals live for sources held only as an excerpt",
    )
    import_canon_parser.add_argument("--source-name", default="agpm-canon")
    import_canon_parser.add_argument("--provided-by", default="project-manager")
    import_canon_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scan and report what would be imported, without touching the store",
    )

    # Append provenance to versions that already exist, without touching them.
    record_provenance_parser = subparsers.add_parser("record-provenance")
    record_provenance_parser.add_argument("--file", type=Path, required=True)

    import_cache_parser = subparsers.add_parser("import-cache")
    import_cache_parser.add_argument("--metadata-dir", type=Path, required=True)
    import_cache_parser.add_argument("--fulltext-dir", type=Path, required=True)

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--scope", choices=sorted(SCOPES), default="current")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--match", choices=list(MATCH_MODES), default="all")

    subparsers.add_parser("coverage-report")

    # Compare a file-store inventory with KX and record the difference (P28).
    reconcile_parser = subparsers.add_parser("reconcile-stores")
    reconcile_parser.add_argument("--inventory", type=Path, required=True)

    # Choose the documents the vertical slice runs on, from the extract that
    # scripts/vertical_slice_candidates.sql produced.
    slice_parser = subparsers.add_parser("vertical-slice")
    slice_parser.add_argument("--candidates", type=Path, required=True)
    slice_parser.add_argument("--size", type=int, default=24)

    # Measure retrieval against a gold set. Prints numbers, never a pass mark.
    eval_parser = subparsers.add_parser("eval-retrieval")
    eval_parser.add_argument("--gold-set", type=Path, required=True)
    eval_parser.add_argument("--k", type=int, default=10)

    # Slice 2.4: source independence. The machine proposes, the owner confirms.
    propose_families_parser = subparsers.add_parser("propose-families")
    propose_families_parser.add_argument("--scope", choices=SCAN_SCOPES, default="perimeter")

    apply_families_parser = subparsers.add_parser("apply-families")
    apply_families_parser.add_argument("--batch", type=Path, required=True)

    propose_duplicates_parser = subparsers.add_parser("propose-duplicates")
    propose_duplicates_parser.add_argument("--scope", choices=SCAN_SCOPES, default="perimeter")
    propose_duplicates_parser.add_argument(
        "--threshold", type=float, default=DEFAULT_SHINGLE_THRESHOLD
    )
    propose_duplicates_parser.add_argument("--width", type=int, default=DEFAULT_SHINGLE_WIDTH)
    propose_duplicates_parser.add_argument("--dry-run", action="store_true")

    confirm_duplicates_parser = subparsers.add_parser("confirm-duplicates")
    confirm_duplicates_parser.add_argument("--batch-id", required=True)
    confirm_duplicates_parser.add_argument("--confirmed-by", required=True)

    independence_parser = subparsers.add_parser("independence")
    independence_parser.add_argument("--scope", choices=SCAN_SCOPES, default="perimeter")

    # Slice 2.6: extraction. The model proposes a verbatim quotation; the offsets
    # are found here, and only an exact span becomes evidence.
    extract_parser = subparsers.add_parser("extract-claims")
    extract_parser.add_argument("--scope", choices=SCAN_SCOPES, default="perimeter")
    extract_parser.add_argument("--limit", type=int, default=20)
    extract_parser.add_argument("--model", choices=sorted(ALLOWED_MODELS), default=None)

    subparsers.add_parser("extraction-report")

    # Slice 2.15: what a better detector would relabel, measured before deciding
    # whether a re-parse is worth its cost.
    subparsers.add_parser("language-drift")

    # Slice 2.5a: the snapshot a knowledge_release_id points at (P27).
    snapshot_parser = subparsers.add_parser("import-wiki-snapshot")
    snapshot_parser.add_argument("--bundle", type=Path, required=True)
    snapshot_parser.add_argument("--perimeter", default="agpm")
    snapshot_parser.add_argument("--notes", default=None)

    snapshots_parser = subparsers.add_parser("wiki-snapshots")
    snapshots_parser.add_argument("--limit", type=int, default=20)

    # Slice 2.5: the wiki as concepts, and evidence bound to its statements.
    concepts_parser = subparsers.add_parser("import-wiki-concepts")
    concepts_parser.add_argument("--snapshot-id", required=True)
    concepts_parser.add_argument("--perimeter", default="agpm")

    bind_parser = subparsers.add_parser("bind-concept-evidence")
    bind_parser.add_argument("--snapshot-id", required=True)
    bind_parser.add_argument("--scope", choices=sorted(SCOPES), default="historical")
    bind_parser.add_argument("--per-statement", type=int, default=5)
    bind_parser.add_argument("--floor", type=float, default=None)

    unsupported_parser = subparsers.add_parser("statements-without-evidence")
    unsupported_parser.add_argument("--snapshot-id", required=True)

    # Slice 2.3: acquisition as a subsystem. A host profile is a decision about
    # how somebody else's server is treated, so it records who made it and why.
    host_profile_parser = subparsers.add_parser("write-host-profile")
    host_profile_parser.add_argument("--host", required=True)
    host_profile_parser.add_argument("--rungs", nargs="*", choices=LADDER, default=["network"])
    host_profile_parser.add_argument(
        "--robots-policy", choices=("respect", "override_recorded"), default="respect"
    )
    host_profile_parser.add_argument("--min-interval-seconds", type=float, default=None)
    host_profile_parser.add_argument("--max-in-flight", type=int, default=None)
    host_profile_parser.add_argument("--rationale", required=True)
    host_profile_parser.add_argument("--decided-by", required=True)

    subparsers.add_parser("host-profiles")

    plan_acquisition_parser = subparsers.add_parser("plan-acquisition")
    plan_acquisition_parser.add_argument("--limit", type=int, default=500)

    subparsers.add_parser("acquisition-gaps")

    # Slice 2.9: candidate ideas. Grouping is deterministic, the gate is
    # arithmetic, and only then does a model write a sentence.
    ideas_parser = subparsers.add_parser("propose-ideas")
    ideas_parser.add_argument("--scope", choices=sorted(SCOPES), default="historical")
    ideas_parser.add_argument("--overlap", type=float, default=DEFAULT_OVERLAP)
    ideas_parser.add_argument("--dry-run", action="store_true")

    subparsers.add_parser("idea-report")

    # Slice 2.8: the structural layer publishes itself when five conditions hold
    # (P19); everything else goes to quarantine with what would clear it.
    publish_parser = subparsers.add_parser("publish-quotes")
    publish_parser.add_argument("--scope", choices=sorted(SCOPES), default="historical")
    publish_parser.add_argument("--limit", type=int, default=200)
    publish_parser.add_argument("--target-language", default="ru")

    translate_parser = subparsers.add_parser("translate-quotes")
    translate_parser.add_argument("--scope", choices=sorted(SCOPES), default="historical")
    translate_parser.add_argument("--target-language", default="ru")
    translate_parser.add_argument("--limit", type=int, default=20)

    subparsers.add_parser("publication-report")

    # Provenance the fetch already recorded, restated in the vocabulary that
    # publication reads. Not a guess: the attempt row is the acquisition record.
    backfill_parser = subparsers.add_parser("backfill-provenance-from-fetches")
    backfill_parser.add_argument("--limit", type=int, default=20000)

    # Slice 2.11: the graph as an immutable projection taken at one moment.
    graph_parser = subparsers.add_parser("build-graph")
    graph_parser.add_argument("--wiki-snapshot-id", default=None)
    graph_parser.add_argument("--dry-run", action="store_true")

    # Slice 3.1: the published slice and the pointer that selects it.
    build_release_parser = subparsers.add_parser("build-release")
    build_release_parser.add_argument("--notes", default=None)
    build_release_parser.add_argument("--dry-run", action="store_true")

    publish_release_parser = subparsers.add_parser("publish-release")
    publish_release_parser.add_argument("--release-id", required=True)
    publish_release_parser.add_argument("--actor", required=True)
    publish_release_parser.add_argument("--rationale", required=True)

    rollback_parser = subparsers.add_parser("rollback-release")
    rollback_parser.add_argument("--actor", required=True)
    rollback_parser.add_argument("--rationale", required=True)

    subparsers.add_parser("active-release")
    subparsers.add_parser("reconcile-release")

    # Slice 2.12: the review queue as something a person can work. Loopback
    # only — KX has no public access (ADR-0005 §16), so reaching it is an SSH
    # tunnel and putting it behind a domain is a separate decision.
    editor_parser = subparsers.add_parser("editor")
    editor_parser.add_argument("--host", default="127.0.0.1")
    editor_parser.add_argument("--port", type=int, default=19702)
    editor_parser.add_argument("--actor", required=True)

    subparsers.add_parser("editor-token")
    subparsers.add_parser("editorial-history")

    # Stage 0a: quotation boundaries. Reports by default, writes only when told.
    repair_spans_parser = subparsers.add_parser("repair-spans")
    repair_spans_parser.add_argument("--apply", action="store_true")
    repair_spans_parser.add_argument("--examples", type=int, default=0)

    # Stage 0a, second half: a publication date, or the radar's own, labelled.
    resolve_dates_parser = subparsers.add_parser("resolve-dates")
    resolve_dates_parser.add_argument("--apply", action="store_true")

    # Local embeddings and the comparison the owner asked for.
    embed_parser = subparsers.add_parser("embed")
    embed_parser.add_argument(
        "--owner-kind", choices=("concept_claim", "claim_evidence"), required=True
    )
    embed_parser.add_argument("--limit", type=int, default=100000)

    compare_parser = subparsers.add_parser("compare-bindings")
    compare_parser.add_argument("--top", type=int, default=5)

    # Slice 2.5в: the owner's own backbone, and the comparison it makes possible.
    load_skeleton_parser = subparsers.add_parser("load-skeleton")
    load_skeleton_parser.add_argument("--file", type=Path, required=True)

    assign_parser = subparsers.add_parser("assign-topics")
    assign_parser.add_argument("--target", choices=("statement", "document"), required=True)
    assign_parser.add_argument("--limit", type=int, default=500)
    assign_parser.add_argument("--batch", type=int, default=25)

    subparsers.add_parser("topic-report")

    compare_topics_parser = subparsers.add_parser("compare-bindings-in-topic")
    compare_topics_parser.add_argument("--top", type=int, default=5)

    queue_parser = subparsers.add_parser("evidence-queue")
    queue_parser.add_argument("--limit", type=int, default=5)

    # Slice 2.14: an answer from the evidence base, or a precise refusal.
    ask_parser = subparsers.add_parser("ask")
    # A question in an instance name does not survive: systemd escapes every
    # non-ASCII byte, and a Russian question comes back as forty \xd0 escapes.
    # A path is ASCII, so free text travels in a file.
    ask_parser.add_argument("question", nargs="?", default=None)
    ask_parser.add_argument("--question-file", type=Path, default=None)
    ask_parser.add_argument("--scope", choices=sorted(SCOPES), default="historical")
    ask_parser.add_argument("--mode", choices=("research", "strict"), default="research")
    ask_parser.add_argument(
        "--asker-scope", choices=("public", "research", "editor"), default="research"
    )
    ask_parser.add_argument("--no-cache", action="store_true")

    # What each kind of model call is allowed to send (ADR-0005 §3). Printing it
    # is how the rule stays inspectable instead of living only in a document.
    subparsers.add_parser("model-run-types")

    # One end-to-end call: orchestrator -> profile -> egress proxy -> provider,
    # and the audit row that proves it happened.
    probe_parser = subparsers.add_parser("model-probe")
    probe_parser.add_argument("--model", choices=sorted(ALLOWED_MODELS), default=None)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--workers", type=int, default=8)

    subparsers.add_parser("status")

    failures_parser = subparsers.add_parser("failures")
    failures_parser.add_argument("--limit", type=int, default=100)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--full", action="store_true")

    requeue_parser = subparsers.add_parser("requeue-failed")
    requeue_parser.add_argument("--error-code")

    perimeter_import_parser = subparsers.add_parser("import-perimeter")
    perimeter_import_parser.add_argument("path", type=Path)

    subparsers.add_parser("perimeter-status")

    perimeter_gaps_parser = subparsers.add_parser("perimeter-gaps")
    perimeter_gaps_parser.add_argument("--limit", type=int, default=500)

    prepare_parser = subparsers.add_parser("perimeter-prepare")
    prepare_parser.add_argument("--robots-override-reason")
    prepare_parser.add_argument("--body-limit-bytes", type=int)
    prepare_parser.add_argument("--requeue", action="store_true")

    reparse_parser = subparsers.add_parser("perimeter-reparse")
    reparse_parser.add_argument("--reason", required=True)

    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = Settings.from_environment()
    database = Database(settings)

    if args.command == "import-manifest":
        manifest = load_manifest(args.path)
        _print_json(database.import_manifest(manifest, source_name=args.source_name))
        return
    if args.command == "import-artifact":
        artifact = load_artifact_manifest(args.manifest)
        _print_json(import_artifact(database, artifact).as_json())
        return
    if args.command == "import-canon":
        canon = scan_canon(args.raw_dir, originals_directory=args.originals_dir)
        if args.dry_run:
            _print_json(canon_summary(canon))
            return
        _print_json(
            import_canon(
                database,
                canon,
                source_name=args.source_name,
                recorded_by=f"radar-kx-import-canon:{args.source_name}",
                provided_by=args.provided_by,
            )
        )
        return
    if args.command == "record-provenance":
        recorded_by, corrections = load_provenance_corrections(args.file)
        _print_json(
            record_provenance_corrections(
                database, recorded_by=recorded_by, corrections=corrections
            )
        )
        return
    if args.command == "import-cache":
        cache_result = import_caches(
            database,
            metadata_dir=args.metadata_dir,
            fulltext_dir=args.fulltext_dir,
        )
        _print_json(dataclasses.asdict(cache_result))
        return
    if args.command == "propose-families":
        proposals = propose_families(database.documents_for_family_proposal(scope=args.scope))
        _print_json(batch_payload(proposals, scope=args.scope))
        return
    if args.command == "apply-families":
        decided_by, decisions = load_family_batch(args.batch)
        _print_json(database.apply_family_batch(decided_by=decided_by, decisions=decisions))
        return
    if args.command == "propose-duplicates":
        texts = database.documents_for_duplicate_scan(scope=args.scope)
        hash_clusters = find_hash_clusters(texts)
        already = frozenset(
            document_id for cluster in hash_clusters for document_id in cluster.document_ids
        )
        shingle_clusters, stats = find_shingle_clusters(
            texts, threshold=args.threshold, width=args.width, exclude=already
        )
        clusters = (*hash_clusters, *shingle_clusters)
        summary: dict[str, Any] = {
            "scope": args.scope,
            "hashClusters": len(hash_clusters),
            "shingleClusters": len(shingle_clusters),
            "shingleScan": stats,
        }
        if args.dry_run:
            summary["clusters"] = [cluster.as_json() for cluster in clusters]
        else:
            summary["recorded"] = database.record_duplicate_proposals(
                clusters, proposed_by=f"radar-kx-propose-duplicates:{args.scope}"
            )
        _print_json(summary)
        return
    if args.command == "confirm-duplicates":
        _print_json(
            {
                "confirmed": database.confirm_duplicate_clusters(
                    batch_id=args.batch_id, confirmed_by=args.confirmed_by
                )
            }
        )
        return
    if args.command == "independence":
        hosts = database.documents_for_family_proposal(scope=args.scope)
        _print_json(
            {
                "scope": args.scope,
                **database.independence_report([item.document_id for item in hosts]),
            }
        )
        return
    if args.command == "extract-claims":
        gateway = ModelGateway(database, settings)
        # A configuration error is not a per-fragment failure. Without this the
        # loop below records one failed run per fragment and the operator learns
        # about a missing key from a thousand identical rows - which is exactly
        # what happened on 2026-08-22 when the pass was started through `kxrun`
        # instead of `kxorch` and the orchestrator env was never read.
        if not settings.hermes_key:
            raise SystemExit(
                "RADAR_KX_HERMES_KEY is not set. Model commands run inside the"
                " orchestrator unit, which reads /etc/radar-kx/orchestrator.env:"
                " use `kxorch extract-claims ...`, not `kxrun`."
            )
        extractor = HermesExtractor(gateway, **({"model": args.model} if args.model else {}))
        fragment_results: list[dict[str, Any]] = []
        for fragment in database.extraction_fragments(scope=args.scope, limit=args.limit):
            failure: str | None = None
            extracted: tuple[ProposedClaim, ...] = ()
            try:
                extracted = extractor.propose(fragment)
            except (ExtractionError, OrchestratorError) as exc:
                failure = f"{type(exc).__name__}: {exc}"
            aligned = align_all(fragment, database.canonical_text(fragment.version_id), extracted)
            fragment_results.append(
                database.record_extraction(
                    fragment,
                    aligned,
                    model=extractor.model,
                    prompt_sha256=prompt_sha256(fragment),
                    failure=failure,
                )
            )
        _print_json(
            {
                "scope": args.scope,
                "fragments": len(fragment_results),
                "claims": sum(int(item.get("claims", 0)) for item in fragment_results),
                "candidates": sum(int(item.get("candidates", 0)) for item in fragment_results),
                "skipped": sum(1 for item in fragment_results if "skipped" in item),
                "fragmentResults": fragment_results,
            }
        )
        return
    if args.command == "extraction-report":
        _print_json(database.extraction_report())
        return
    if args.command == "language-drift":
        _print_json(database.language_drift())
        return
    if args.command == "import-wiki-snapshot":
        snapshot = read_bundle(args.bundle, perimeter=args.perimeter)
        _print_json(
            database.record_wiki_snapshot(
                snapshot,
                recorded_by=f"radar-kx-import-wiki-snapshot:{args.perimeter}",
                notes=args.notes,
            )
        )
        return
    if args.command == "wiki-snapshots":
        _print_json(database.wiki_snapshots(limit=args.limit))
        return
    if args.command == "import-wiki-concepts":
        _print_json(
            database.import_wiki_concepts(
                snapshot_id=args.snapshot_id,
                perimeter=args.perimeter,
                imported_by=f"radar-kx-import-wiki-concepts:{args.perimeter}",
            )
        )
        return
    if args.command == "bind-concept-evidence":
        _print_json(
            database.bind_concept_evidence(
                snapshot_id=args.snapshot_id,
                scope=args.scope,
                per_statement=args.per_statement,
                created_by=f"radar-kx-bind-concept-evidence:{args.scope}",
                **({"floor": args.floor} if args.floor is not None else {}),
            )
        )
        return
    if args.command == "statements-without-evidence":
        _print_json(database.statements_without_evidence(snapshot_id=args.snapshot_id))
        return
    if args.command == "write-host-profile":
        _print_json(
            database.write_host_profile(
                HostProfile(
                    host=args.host.lower(),
                    rungs=tuple(args.rungs),
                    min_interval_seconds=args.min_interval_seconds,
                    max_in_flight=args.max_in_flight,
                    robots_policy=args.robots_policy,
                    rationale=args.rationale,
                    decided_by=args.decided_by,
                )
            )
        )
        return
    if args.command == "host-profiles":
        _print_json([profile.as_json() for profile in database.host_profiles().values()])
        return
    if args.command == "plan-acquisition":
        _print_json(database.plan_acquisition(limit=args.limit))
        return
    if args.command == "acquisition-gaps":
        _print_json(database.acquisition_gaps())
        return
    if args.command == "propose-ideas":
        judged = database.propose_candidate_groups(scope=args.scope, threshold=args.overlap)
        verdicts = {group.fingerprint: verdict for group, verdict in judged}
        overview = summarize([group for group, _ in judged], verdicts)
        if args.dry_run:
            _print_json(
                {
                    "scope": args.scope,
                    **overview,
                    "groups_detail": [
                        {**group.as_json(), **verdict.as_json()} for group, verdict in judged
                    ][:20],
                }
            )
            return
        if not settings.hermes_key:
            raise SystemExit(
                "RADAR_KX_HERMES_KEY is not set. Use `kxorch propose-ideas ...`, not `kxrun`."
            )
        gateway = ModelGateway(database, settings)
        written: list[dict[str, Any]] = []
        for group, verdict in judged:
            if not verdict.admitted:
                # Recorded, never phrased: P13 says it is not shown, and asking a
                # model to write a sentence nobody will read is a call for nothing.
                written.append(
                    database.record_idea(
                        group,
                        verdict,
                        title="(not shown: fewer than two independent sources)",
                        statement=" | ".join(claim.quote_text[:120] for claim in group.claims),
                        created_by="radar-kx-propose-ideas",
                    )
                )
                continue
            result = gateway.run(IDEA_STATEMENT, build_idea_prompt(group))
            title, statement = parse_idea(result.content)
            written.append(
                database.record_idea(
                    group,
                    verdict,
                    title=title,
                    statement=statement,
                    created_by="radar-kx-propose-ideas",
                    model=IDEA_STATEMENT.model,
                    prompt_sha256=idea_prompt_sha256(group),
                )
            )
        _print_json({"scope": args.scope, **overview, "recorded": written})
        return
    if args.command == "idea-report":
        _print_json(database.idea_report())
        return
    if args.command == "publish-quotes":
        _print_json(
            database.publish_quotes(
                scope=args.scope, limit=args.limit, target_language=args.target_language
            )
        )
        return
    if args.command == "publication-report":
        _print_json(database.publication_report())
        return
    if args.command == "backfill-provenance-from-fetches":
        _print_json(database.backfill_provenance_from_fetches(limit=args.limit))
        return
    if args.command == "build-release":
        if args.dry_run:
            _print_json(database.compose_release().as_json())
            return
        _print_json(database.build_release(built_by="radar-kx-build-release", notes=args.notes))
        return
    if args.command == "publish-release":
        _print_json(
            database.publish_release(args.release_id, actor=args.actor, rationale=args.rationale)
        )
        return
    if args.command == "rollback-release":
        _print_json(database.rollback_release(actor=args.actor, rationale=args.rationale))
        return
    if args.command == "active-release":
        _print_json(database.active_release() or {"active": None})
        return
    if args.command == "reconcile-release":
        _print_json(database.reconcile_release())
        return
    if args.command == "ask":
        if args.question_file is not None:
            args.question = args.question_file.read_text(encoding="utf-8").strip()
        if not args.question:
            raise SystemExit("give a question, or --question-file with one in it")
        if not args.no_cache:
            cached = database.cached_answer(args.question, scope=args.asker_scope)
            if cached is not None:
                _print_json({**cached, "fromCache": True})
                return
        package = database.evidence_for_question(args.question, scope=args.scope)
        if not package:
            # ADR-0004 §9 and §10: a structural refusal with a precise code, not a
            # hedged sentence. There is nothing nearby either, because nothing came
            # back for the question at all.
            refusal = refuse("no_evidence", "nothing in the evidence base matched the question")
            _print_json(
                {
                    **refusal.as_json(),
                    **database.record_answer(
                        question=args.question,
                        scope=args.asker_scope,
                        mode=args.mode,
                        package=(),
                        refusal=refusal,
                        answered_by="radar-kx-ask",
                    ),
                }
            )
            return
        if not settings.hermes_key:
            raise SystemExit("RADAR_KX_HERMES_KEY is not set. Use `kxorch ask ...`.")
        gateway = ModelGateway(database, settings)
        result = gateway.run(RESEARCH_ANSWER, build_answer_prompt(args.question, package))
        clauses = parse_research_answer(result.content)
        answer_check = verify(clauses, package, mode=args.mode)
        if not clauses or not answer_check.passes:
            # The draft did not survive checking. A refusal is the honest outcome,
            # and what the base does support nearby is retrieved for the question -
            # it is the same package - and returned as its own field so nothing can
            # merge it into a paragraph that reads like an answer (§9a).
            refusal = refuse(
                "no_evidence",
                "no clause survived verification against the cited spans"
                if clauses
                else "the evidence does not answer the question",
                package,
            )
            _print_json(
                {
                    **refusal.as_json(),
                    "verification": answer_check.as_json(),
                    **database.record_answer(
                        question=args.question,
                        scope=args.asker_scope,
                        mode=args.mode,
                        package=package,
                        refusal=refusal,
                        verification=answer_check,
                        model=RESEARCH_ANSWER.model,
                        prompt_sha256=answer_prompt_sha256(args.question, package),
                        answered_by="radar-kx-ask",
                    ),
                }
            )
            return
        answer = render(clauses)
        _print_json(
            {
                "answer": answer,
                "evidence": [element.as_json() for element in package],
                "verification": answer_check.as_json(),
                **database.record_answer(
                    question=args.question,
                    scope=args.asker_scope,
                    mode=args.mode,
                    package=package,
                    answer_text=answer,
                    verification=answer_check,
                    model=RESEARCH_ANSWER.model,
                    prompt_sha256=answer_prompt_sha256(args.question, package),
                    answered_by="radar-kx-ask",
                ),
            }
        )
        return
    if args.command == "editor-token":
        print(generate_token())
        return
    if args.command == "evidence-queue":
        _print_json(database.evidence_queue(limit=args.limit))
        return
    if args.command == "repair-spans":
        repairs = database.plan_span_repair()
        report = summarize_spans(repairs)
        if args.examples:
            moved = [repair for repair in repairs if repair.changed]
            step = max(1, len(moved) // args.examples)
            report["examples"] = [repair.as_example() for repair in moved[::step][: args.examples]]
        if args.apply:
            report.update(database.apply_span_repair(repairs))
        else:
            report["applied"] = False
        _print_json(report)
        return
    if args.command == "resolve-dates":
        dates = database.plan_document_dates()
        report = summarize_dates(dates)
        if args.apply:
            report.update(database.apply_document_dates(dates))
        else:
            report["applied"] = False
        _print_json(report)
        return
    if args.command == "embed":
        _print_json(database.embed(args.owner_kind, limit=args.limit))
        return
    if args.command == "compare-bindings":
        _print_json(database.compare_binding_methods(top=args.top))
        return
    if args.command == "load-skeleton":
        _print_json(database.adopt_authored_skeleton(load_authored_skeleton(args.file)))
        return
    if args.command == "topic-report":
        _print_json(database.topic_assignment_report())
        return
    if args.command == "compare-bindings-in-topic":
        _print_json(database.compare_binding_methods_within_topics(top=args.top))
        return
    if args.command == "assign-topics":
        if not settings.hermes_key:
            raise SystemExit("RADAR_KX_HERMES_KEY is not set. Use `kxorch assign-topics ...`.")
        # Level 2 is the grain the model is asked for: twelve categories are too
        # coarse to place anything and a hundred leaves are too many to hold in
        # one prompt. The level-1 section each one belongs to travels with it, and
        # that is what the comparison scopes by.
        rubricator = build_rubricator(database.topics(level=2))
        allowed = frozenset(topic["topic_key"] for topic in database.topics(level=2))
        gateway = ModelGateway(database, settings)
        items = database.unassigned_topic_items(args.target, limit=args.limit)
        placed = 0
        dropped = {"unknownTopic": 0, "unknownItem": 0, "overCap": 0}
        without_topic = 0
        for start in range(0, len(items), args.batch):
            block = items[start : start + args.batch]
            result = gateway.run(TOPIC_ASSIGNMENT, build_payload(block), system=rubricator)
            assignments, thrown = parse_assignment(result.content, block, allowed)
            for key, value in thrown.items():
                dropped[key] += value
            without_topic += sum(1 for item in assignments if not item.topic_keys)
            placed += database.record_topic_assignments(
                args.target, assignments, assigned_by=f"{TOPIC_ASSIGNMENT.model}"
            )
        _print_json(
            {
                "target": args.target,
                "items": len(items),
                "assignments": placed,
                "itemsTheRubricatorDoesNotCover": without_topic,
                "dropped": dropped,
            }
        )
        return
    if args.command == "editorial-history":
        _print_json(database.editorial_history())
        return
    if args.command == "editor":
        token = os.environ.get("RADAR_KX_EDITOR_TOKEN", "")
        if not token:
            raise SystemExit(
                "RADAR_KX_EDITOR_TOKEN is not set. Generate one with"
                " `radar_kx editor-token` and put it in /etc/radar-kx/editor.env."
            )
        server = serve(
            settings,
            host=args.host,
            port=args.port,
            token=token,
            actor=args.actor,
            # Basic auth for a person in a browser; the bearer token stays for the
            # loopback and scripted paths. Both are checked by the service itself.
            username=os.environ.get("RADAR_KX_EDITOR_USER") or None,
            password=os.environ.get("RADAR_KX_EDITOR_PASSWORD") or None,
        )
        print(f"editor on http://{args.host}:{args.port}/?token=<your token>")
        with server:
            server.serve_forever()
        return
    if args.command == "build-graph":
        graph = database.build_graph(wiki_snapshot_id=args.wiki_snapshot_id)
        if args.dry_run:
            _print_json({**graph.as_json(), "unsupported": unsupported(graph)})
            return
        _print_json(
            database.record_graph_snapshot(
                graph,
                built_by="radar-kx-build-graph",
                wiki_snapshot_id=args.wiki_snapshot_id,
            )
        )
        return
    if args.command == "translate-quotes":
        if not settings.hermes_key:
            raise SystemExit("RADAR_KX_HERMES_KEY is not set. Use `kxorch translate-quotes ...`.")
        gateway = ModelGateway(database, settings)
        done: list[dict[str, Any]] = []
        for row in database.publishable_quotes(scope=args.scope, limit=args.limit):
            if row["translation_id"] is not None:
                continue
            if str(row["language"]) == args.target_language:
                continue
            quote = str(row["quote_text"])
            prompt = build_translation_prompt(quote, target_language=args.target_language)
            result = gateway.run(
                QUOTE_TRANSLATION,
                prompt,
                version_id=str(row["version_id"]),
            )
            translated = parse_translation(result.content)
            done.append(
                database.record_translation(
                    claim_id=str(row["claim_id"]),
                    version_id=str(row["version_id"]),
                    char_start=int(row["char_start"]),
                    char_end=int(row["char_end"]),
                    original_text=quote,
                    source_language=str(row["language"]),
                    target_language=args.target_language,
                    translated_text=translated,
                    translator=QUOTE_TRANSLATION.model,
                    is_machine=True,
                    prompt_sha256=sha256_bytes(prompt.encode("utf-8")),
                    report=check_invariants(quote, translated),
                    created_by="radar-kx-translate-quotes",
                )
            )
        _print_json(
            {
                "targetLanguage": args.target_language,
                "translated": len(done),
                "verified": sum(1 for item in done if item["state"] == "verified"),
                "rejected": sum(1 for item in done if item["state"] == "rejected"),
                "aliasProposals": sum(int(item["aliasProposals"]) for item in done),
            }
        )
        return
    if args.command == "model-run-types":
        _print_json([run_type.as_json() for run_type in RUN_TYPES.values()])
        return
    if args.command == "model-probe":
        _print_json(ModelGateway(database, settings).probe(model=args.model).as_json())
        return
    if args.command == "run":
        _print_json(run_until_idle(settings, workers=args.workers))
        return
    if args.command == "search":
        _print_json(
            {
                "query": args.query,
                "scope": args.scope,
                "hits": [
                    hit.as_json()
                    for hit in database.search(
                        args.query, scope=args.scope, limit=args.limit, match=args.match
                    )
                ],
            }
        )
        return
    if args.command == "vertical-slice":
        payload = json.loads(args.candidates.read_text(encoding="utf-8"))
        _print_json(select_slice(load_candidates(payload), size=args.size).as_json())
        return
    if args.command == "eval-retrieval":
        name, questions = load_gold_set(args.gold_set)
        results, summary = evaluate(database.search, questions, k=args.k)
        _print_json(
            {
                "goldSet": name,
                "summary": summary,
                "results": [item.as_json() for item in results],
            }
        )
        return
    if args.command == "reconcile-stores":
        scope, entries, source = load_inventory(args.inventory)
        _print_json(
            database.record_store_reconciliation(
                scope,
                entries,
                source=source,
                generated_by=f"radar_kx reconcile-stores:{settings.release_id}",
            )
        )
        return
    if args.command == "coverage-report":
        coverage = database.coverage_report()
        _print_json(coverage)
        raise SystemExit(0 if coverage["status"] == "ok" else 1)
    if args.command == "status":
        _print_json(database.status())
        return
    if args.command == "failures":
        _print_json(list(database.iter_failures(limit=args.limit)))
        return
    if args.command == "verify":
        verification = database.verify(full=args.full)
        _print_json(verification)
        raise SystemExit(0 if verification["status"] == "ok" else 1)
    if args.command == "requeue-failed":
        _print_json(
            {
                "requeued": database.requeue_failed(error_code=args.error_code),
                "errorCode": args.error_code,
            }
        )
        return
    if args.command == "import-perimeter":
        _print_json(database.import_issue_perimeter(load_perimeter_export(args.path)))
        return
    if args.command == "perimeter-status":
        _print_json(database.perimeter_status())
        return
    if args.command == "perimeter-gaps":
        _print_json(list(database.iter_perimeter_gaps(limit=args.limit)))
        return
    if args.command == "perimeter-prepare":
        _print_json(
            database.prepare_perimeter(
                robots_override_reason=args.robots_override_reason,
                body_limit_bytes=args.body_limit_bytes,
                requeue=args.requeue,
            )
        )
        return
    if args.command == "perimeter-reparse":
        _print_json(
            database.reparse_perimeter_gaps(
                reason=args.reason,
                min_text_chars=settings.min_text_chars,
            )
        )
        return
    raise AssertionError(f"unhandled command: {args.command}")
