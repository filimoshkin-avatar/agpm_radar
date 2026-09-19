"""Behavioral regressions for event selection, evidence and publication boundaries."""

from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from packages.contracts.event_dedup import (
    EventDedupError,
    cheap_deduplicate,
    compare_events,
    deduplicate,
    publication_gate,
    url_key,
    validate_decision,
    validate_passport,
)
from packages.domain.candidate_mutations import (
    _validate_correction_preconditions,
    issue_state_hash,
)
from packages.domain.candidate_package import build_candidate_package
from packages.domain.candidates import validate_candidate
from packages.storage.event_registry import published_events
from packages.validation.public_issue import (
    build_public_issue,
    validate_public_issue_document,
)
from tools.build_stage14_daily import _material
from tools.event_pipeline import (
    ARBITER_PROMPT_VERSION,
    MODEL,
    PROMPT_VERSION,
    EventModel,
    _strict_json,
    attach_report_evidence,
    calibrated,
    load_history,
    prepare_events,
    report_manifest,
)

from test_stage5_candidate_builder import _daily_candidate, _seed_database, _snapshot_workspace

ROOT = Path(__file__).resolve().parents[1]


def event(**changes: Any) -> dict[str, Any]:
    return {
        "subject": "Anthropic",
        "action": "launch",
        "object": "Claude for Financial Advisors",
        "event_date": "2026-09-14",
        "products": ["Claude for Financial Advisors"],
        "organizations": ["Anthropic", "Schwab", "BlackRock"],
        "event_type": "launch",
        "evidence": "Anthropic launched Claude for Financial Advisors on September 14, 2026.",
        "facts": ["Schwab and BlackRock are partners."],
        "card_title": "Anthropic представила Claude для финансовых консультантов",
        "card_summary": "Anthropic представила продукт для финансовых консультантов.",
        "strong_signal": True,
        **changes,
    }


def material(
    name: str,
    *,
    source: str = "news",
    fulltext: bool = True,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected = events if events is not None else [event()]
    text = " ".join(part for value in selected for part in [value["evidence"], *value["facts"]])
    return {
        "id": name,
        "url": f"https://{name}.example/story",
        "title": f"Unique headline {name}",
        "summary": text,
        "raw_excerpt": text,
        "_fulltext_status": "resolved" if fulltext else "unresolved",
        "published_at": "2026-09-15",
        "event_passport": {
            "source_type": source,
            "events": selected,
            "non_event_reason": "" if selected else "No event",
        },
    }


def decision(same: bool = True, confidence: float = 0.99) -> dict[str, Any]:
    return {
        "same_event": same,
        "confidence": confidence,
        "common_event": "The product launch" if same else "",
        "unique_facts_left": [],
        "unique_facts_right": [],
        "recommended_source": "left",
        "explanation": "Compared the action and the dated source evidence.",
    }


@pytest.mark.parametrize(
    "variant",
    [
        "https://www.example.com/en/news/amp?utm_source=x",
        "http://example.com/ru/news?amp=1",
        "https://example.com/news.amp?output=amp",
        "https://example.com/news/?locale=ru#top",
    ],
)
def test_url_variants_are_one_exact_key(variant: str) -> None:
    assert url_key(variant) == "https://example.com/news"
    assert url_key("https://example.com/news?id=1") != url_key("https://example.com/news?id=2")


def test_three_stories_one_event_best_primary_and_all_links() -> None:
    items = [
        material("news"),
        material("primary", source="primary"),
        material("review", source="review"),
    ]
    original = copy.deepcopy(items)
    cards, audit = deduplicate(items)
    assert items == original
    assert len(cards) == 1 and cards[0]["id"] == "primary"
    assert cards[0]["alternative_links"] == [items[0]["url"], items[2]["url"]]
    assert not audit["pending"]
    selected = cards[0]["event_dedup"]["events"][0]
    assert selected["members"] == ["news", "primary", "review"]
    assert selected["selection_reason"]


def test_unavailable_primary_cannot_beat_verified_news() -> None:
    cards, _ = deduplicate(
        [material("primary", source="primary", fulltext=False), material("news")]
    )
    assert cards[0]["id"] == "news"


def test_representative_counts_facts_about_the_event_not_the_entire_review() -> None:
    richer = event(facts=["Schwab and BlackRock are partners.", "Audited workflows are supported."])
    cards, _ = deduplicate([material("news"), material("review", source="review", events=[richer])])
    assert cards[0]["id"] == "review"
    assert cards[0]["alternative_links"] == ["https://news.example/story"]


def test_pending_model_merge_writes_audit_and_stops_before_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    _seed_database(source)
    items = [material("a"), material("b", events=[event(event_date="2026-09-15")])]
    by_url = {item["url"]: item for item in items}

    def inference(prompt: str) -> object:
        context = json.loads(prompt.split("\nSOURCE JSON:\n", 1)[1])
        return by_url[context["url"]]["event_passport"] if "url" in context else decision()

    audit = tmp_path / "report.event-audit.json"
    with pytest.raises(EventDedupError, match="require review"):
        prepare_events(
            items,
            cache=tmp_path / "event-inference",
            issue_day="2026-09-19",
            source_db=source,
            audit_path=audit,
            inference=inference,
            use_history=False,
        )
    result = json.loads(audit.read_text())
    assert result["pending"] and not result["llm_auto_merge_calibrated"]
    assert not list(tmp_path.glob("*.events.json"))


def test_review_projects_only_unique_strong_event() -> None:
    unique = event(
        subject="OtherCo",
        object="Risk pilot",
        action="deployment",
        event_type="deployment",
        products=["Risk pilot"],
        organizations=["OtherCo"],
        evidence="OtherCo deployed Risk pilot.",
        facts=["OtherCo deployed Risk pilot."],
        card_title="Запущен пилот управления рисками",
        card_summary="OtherCo начала пилот управления рисками.",
    )
    review = material("review", source="review", events=[event(), unique])
    cards, audit = deduplicate([review, material("primary", source="primary")])
    assert not audit["pending"]
    assert len(cards) == 2
    result = next(card for card in cards if card["id"] == "review")
    assert result["title"] == unique["card_title"]
    assert result["summary"] == unique["card_summary"]
    assert "Anthropic" not in result["raw_excerpt"]
    assert len(result["event_dedup"]["events"]) == 1


def test_review_without_unique_value_is_source_only() -> None:
    cards, _ = deduplicate(
        [material("review", source="review", events=[event(strong_signal=False)])]
    )
    assert cards == []


def test_new_feature_is_child_not_duplicate_of_launch() -> None:
    old, _ = deduplicate([material("original")])
    new = event(
        action="feature",
        event_type="feature",
        object="Claude advisor audit trail",
        event_date="2026-09-18",
    )
    cards, _ = deduplicate([material("feature", events=[new])], history=[old[0]["event_dedup"]])
    assert len(cards) == 1
    child = cards[0]["event_dedup"]["events"][0]
    assert (
        child["parent_event_cluster_id"] == old[0]["event_dedup"]["events"][0]["event_cluster_id"]
    )
    assert child["event_cluster_id"] != child["parent_event_cluster_id"]


def test_later_article_about_old_launch_is_suppressed() -> None:
    old, _ = deduplicate([material("old")])
    new = material("new")
    new["published_at"] = "2026-09-19"
    cards, audit = deduplicate([new], history=[old[0]["event_dedup"]])
    assert not cards
    assert any(row["reason"] == "published_in_registry" for row in audit["suppressed"])


def test_observe_correction_does_not_consult_blocking_event_history(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    _seed_database(source)
    cards, _ = deduplicate([material("new-url")])
    with sqlite3.connect(source) as connection:
        candidate = {
            "targetIssueDate": "2026-08-19",
            "expectedIssueStateHash": issue_state_hash(connection, "issue_20260819"),
            "sharedMaterialPreconditions": [],
            "desiredIssue": {
                "issueId": "issue_20260819",
                "issueDate": "2026-08-19",
                "materials": cards,
            },
        }
        with patch(
            "packages.domain.candidate_mutations.published_events",
            return_value=[cards[0]["event_dedup"]],
        ) as registry:
            _validate_correction_preconditions(connection, candidate)
            registry.assert_not_called()


def test_semantic_similarity_cannot_merge_different_products() -> None:
    left, right = event(), event(object="Claude Enterprise", products=["Claude Enterprise"])
    result, _ = compare_events(left, right, lambda *_: decision())
    assert result == "review"


def test_dates_do_not_chain_transitively() -> None:
    items = [
        material(str(day), events=[event(event_date=f"2026-09-{day:02}")]) for day in (1, 7, 13)
    ]
    _, audit = deduplicate(items, arbiter=lambda *_: decision(), allow_llm_merge=True)
    assert audit["pending"]


def test_medium_confidence_requires_independent_confirmation() -> None:
    calls: list[int] = []

    def arbitrate(left: dict[str, Any], right: dict[str, Any], attempt: int) -> object:
        calls.append(attempt)
        return decision(confidence=0.8 if attempt == 1 else 0.99)

    result, _ = compare_events(
        event(), event(event_date="2026-09-15"), arbitrate, allow_llm_merge=True
    )
    assert result == "merge" and calls == [1, 2]


def test_disagreement_requires_review_and_low_confidence_keeps_separate() -> None:
    assert (
        compare_events(
            event(),
            event(event_date="2026-09-15"),
            lambda _l, _r, attempt: decision(attempt == 1, 0.8 if attempt == 1 else 0.99),
        )[0]
        == "review"
    )
    assert (
        compare_events(
            event(), event(event_date="2026-09-15"), lambda *_: decision(confidence=0.4)
        )[0]
        == "separate"
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "0.99", -1, 1.1])
def test_strict_arbiter_confidence(bad: Any) -> None:
    with pytest.raises(EventDedupError):
        validate_decision(decision() | {"confidence": bad})


def test_missing_date_never_gets_inferred_from_article_date() -> None:
    assert compare_events(event(event_date=None), event(), lambda *_: decision())[0] == "review"


def test_real_fallback_action_mismatch_cannot_bypass_arbitration() -> None:
    pair = json.loads((ROOT / "tests/data/event-moneycontrol-yahoo-20260919.json").read_text())
    calls: list[int] = []

    def arbiter(left: dict[str, Any], right: dict[str, Any], attempt: int) -> object:
        calls.append(attempt)
        return decision()

    assert compare_events(pair["left"], pair["right"], arbiter)[0] == "review"
    assert calls == [1]
    assert compare_events(pair["left"], pair["right"])[0] == "review"


def test_evidence_must_actually_exist_in_text() -> None:
    with pytest.raises(EventDedupError, match="verbatim"):
        validate_passport(material("a")["event_passport"], "Unrelated article")


def test_strict_json_rejects_duplicate_keys_and_nan() -> None:
    for value in ('{"same_event":true,"same_event":false}', '{"confidence":NaN}'):
        with pytest.raises(EventDedupError):
            _strict_json(value)


def test_publication_blocks_duplicate_id_event_and_unexplained_cluster() -> None:
    cards, _ = deduplicate([material("a")])
    with pytest.raises(EventDedupError):
        publication_gate([cards[0], cards[0]])
    unexplained = copy.deepcopy(cards[0])
    unexplained["event_dedup"]["events"][0]["selection_reason"] = ""
    with pytest.raises(EventDedupError, match="selection_reason"):
        publication_gate([unexplained])
    with pytest.raises(EventDedupError, match="missing passport"):
        publication_gate([material("raw")], require_events=True)


def test_fallback_cannot_overlap_verified_event_under_another_id() -> None:
    a, _ = deduplicate([material("a")])
    b, _ = deduplicate([material("b", fulltext=False)])
    b[0]["event_dedup"]["events"][0]["event_cluster_id"] = "evt_" + "b" * 24
    with pytest.raises(EventDedupError, match="overlapping"):
        publication_gate([a[0], b[0]])


def test_model_cache_binds_fulltext_prompt_and_model(tmp_path: Path) -> None:
    calls = []
    item = material("a")

    def inference(prompt: str) -> object:
        calls.append(prompt)
        return item["event_passport"]

    model = EventModel(tmp_path, inference)
    assert model.extract(item) == model.extract(item)
    assert len(calls) == 1
    model.extract(item | {"raw_excerpt": item["raw_excerpt"] + " Additional source fact."})
    assert len(calls) == 2


def test_manifest_binds_checked_document_and_legacy_export(tmp_path: Path) -> None:
    cards, _ = deduplicate([material("a")])
    stem = tmp_path / "AgPM_daily_radar_2026-09-19"
    stem.with_suffix(".md").write_bytes(b"markdown")
    stem.with_suffix(".docx").write_bytes(b"docx")
    manifest = report_manifest(cards, b"markdown", b"docx", "2026-09-19")
    stem.with_suffix(".events.json").write_text(json.dumps(manifest))
    exported = [{"url": cards[0]["url"], "title": cards[0]["title"], "id": "export"}]
    attach_report_evidence(exported, tmp_path, "2026-09-19")
    assert exported[0]["event_dedup"] == cards[0]["event_dedup"]
    stem.with_suffix(".docx").write_bytes(b"changed")
    with pytest.raises(EventDedupError, match="changed"):
        attach_report_evidence(exported, tmp_path, "2026-09-19")


def test_registry_uses_45_days_only_published_not_drafts_or_same_day() -> None:
    cards, _ = deduplicate([material("a")])
    envelope = cards[0]["event_dedup"]
    with sqlite3.connect(":memory:") as db:
        db.executescript(
            "CREATE TABLE material_evidence(material_id,kind,metadata_json); CREATE TABLE issue_materials(material_id,issue_id); CREATE TABLE issues(issue_id,issue_date,lifecycle_status);"
        )
        for index, (day, status) in enumerate(
            [
                ("2026-08-05", "published"),
                ("2026-08-04", "published"),
                ("2026-09-18", "draft"),
                ("2026-09-19", "published"),
            ]
        ):
            issue_id = str(index)
            db.execute("INSERT INTO issues VALUES(?,?,?)", (issue_id, day, status))
            db.execute("INSERT INTO issue_materials VALUES(?,?)", (issue_id, issue_id))
            db.execute(
                "INSERT INTO material_evidence VALUES(?,?,?)",
                (
                    issue_id,
                    "event_dedup",
                    json.dumps({"issue_id": issue_id, "event_dedup": envelope}),
                ),
            )
        assert published_events(db, "2026-09-19") == [envelope]


def test_legacy_stonly_card_renders_once_across_sections() -> None:
    # Legacy dependencies are installed in its system interpreter, not V2's venv.
    code = """
import sys
from datetime import datetime, timezone
sys.path.insert(0, sys.argv[1])
import agpm_radar_report as report
item = {"id":"stonly", "title":"Stonly launches an enterprise workflow agent", "url":"https://stonly.example/launch", "summary":"Agent workflows", "source_hits":[{"source_id":"ai_agents_directory_daily", "hit_url":"https://directory.example/daily"}], "_radar_review":{"perimeter":"far","verdict":"core","score":20}}
now=datetime(2026,9,19,tzinfo=timezone.utc)
text=report.render_markdown([item], now, now, [item], [])
assert text.count("### Stonly launches") == 1, text
assert text.count("Ссылка: https://stonly.example/launch") == 1, text
assert report.event_key({"url":"https://a.example/a", "title":"agent orchestration"}) != report.event_key({"url":"https://b.example/b", "title":"agent orchestration"})
"""
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/python3", "-c", code, str(ROOT.parent / "pipeline/scripts")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_event_evidence_survives_replay_but_does_not_block_observe_publication(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite"
    state_hash = _seed_database(source)
    workspace, attestation = _snapshot_workspace(tmp_path)
    candidate: Any = _daily_candidate(state_hash, attestation.identity)
    selected, _ = deduplicate([material("a", events=[event(event_date="2026-08-19")])])
    raw = selected[0] | {
        "publication_date_status": "resolved",
        "published_at": "2026-08-19",
        "summary": "Запущен продукт для финансовых консультантов.",
        "perimeter": "far",
        "verdict": "core",
        "signal_strength": "strong",
    }
    candidate["desiredIssue"]["materials"] = [_material(raw, 1)]
    candidate["desiredIssue"]["emptyReason"] = None
    candidate["desiredIssue"]["stats"] = {
        "viewed": 1,
        "included": 1,
        "cut": 0,
        "near": 0,
        "mid": 0,
        "far": 1,
        "core": 1,
        "adjacent": 0,
    }
    result = build_candidate_package(
        source_database=source,
        staging_database=tmp_path / "staging.sqlite",
        package_store=tmp_path / "packages",
        candidate=candidate,
        v2_workspace=workspace,
    )
    with sqlite3.connect(result.replay.staging_path) as connection:
        history = published_events(connection, "2026-08-21")
        assert history == [selected[0]["event_dedup"]]
        public = build_public_issue(connection, issue_date="2026-08-20")
        assert public["materialCount"] == 1
        metadata = json.loads(
            connection.execute(
                "SELECT metadata_json FROM material_evidence WHERE kind='event_dedup'"
            ).fetchone()[0]
        )
        metadata["event_dedup"]["events"][0]["selection_reason"] = ""
        connection.execute(
            "UPDATE material_evidence SET metadata_json=? WHERE kind='event_dedup'",
            (json.dumps(metadata),),
        )
        assert build_public_issue(connection, issue_date="2026-08-20") == public
    current = copy.deepcopy(candidate)
    current["desiredIssue"]["issueDate"] = "2026-09-19"
    current["desiredIssue"]["materials"][0].pop("eventDedup")
    validate_candidate(current)


def test_cheap_copies_keep_verified_body_and_discovery_links() -> None:
    a, b = material("a", fulltext=False), material("b")
    a["url"] = "https://example.com/en/story?utm_source=feed"
    b["url"] = "https://example.com/story/amp"
    a["source_hits"] = [{"source_id": "directory"}]
    cards = cheap_deduplicate([a, b])
    assert len(cards) == 1 and cards[0]["id"] == "b"
    assert cards[0]["source_hits"] == a["source_hits"]
    assert cards[0]["exact_members"] == ["a", "b"]
    selected, _ = deduplicate(cards)
    assert selected[0]["event_dedup"]["events"][0]["members"] == ["a", "b"]


def test_llm_merging_requires_reviewed_calibration(tmp_path: Path) -> None:
    path = tmp_path / "calibration.json"
    assert not calibrated(path)
    assert (
        compare_events(event(), event(event_date="2026-09-15"), lambda *_: decision())[0]
        == "review"
    )
    path.write_text(
        json.dumps(
            {
                "policy_version": "events-v1",
                "prompt_version": PROMPT_VERSION,
                "arbiter_prompt_version": ARBITER_PROMPT_VERSION,
                "model": MODEL,
                "high_confidence": 0.95,
                "medium_confidence": 0.75,
                "false_merges": 0,
                "evaluated_pairs": 80,
                "reviewed_by": "test reviewer",
                "dataset_sha256": "a" * 64,
            }
        )
    )
    assert calibrated(path)
    path.write_text(path.read_text().replace(MODEL, "different/model"))
    with pytest.raises(EventDedupError, match="calibration"):
        calibrated(path)


def test_cold_registry_is_warmed_from_published_history_and_resumes_from_cache(
    tmp_path: Path,
) -> None:
    item = material("old")
    texts = tmp_path / "source-fulltext"
    texts.mkdir()
    (texts / "old.json").write_text(
        json.dumps({"url": item["url"], "status": "resolved", "text": item["raw_excerpt"]})
    )
    calls: list[str] = []

    def inference(prompt: str) -> object:
        calls.append(prompt)
        assert item["raw_excerpt"] in prompt
        return item["event_passport"]

    with sqlite3.connect(":memory:") as db:
        db.executescript(
            "CREATE TABLE materials(material_id,title,url,published_at,summary); CREATE TABLE issues(issue_id,issue_date,lifecycle_status); CREATE TABLE issue_materials(material_id,issue_id,summary); CREATE TABLE material_evidence(material_id,kind,metadata_json);"
        )
        db.execute(
            "INSERT INTO materials VALUES(?,?,?,?,?)",
            ("old", item["title"], item["url"], "2026-09-15", "Old summary"),
        )
        db.execute("INSERT INTO issues VALUES('old','2026-09-15','published')")
        db.execute("INSERT INTO issue_materials VALUES('old','old','Old summary')")
        cache = tmp_path / "event-inference"
        history = load_history(db, "2026-09-19", EventModel(cache, inference), texts)
        assert len(history) == 1 and len(calls) == 1
        assert history == load_history(db, "2026-09-19", EventModel(cache, inference), texts)
        assert len(calls) == 1
        cards, audit = deduplicate([material("later")], history=history)
        assert not cards and not audit["pending"]


def test_matching_titles_are_nonblocking_in_observe_mode() -> None:
    golden = json.loads((ROOT / "fixtures/synthetic/stage6-golden.json").read_text())
    document = copy.deepcopy(
        next(
            row["document"]
            for row in golden.values()
            if isinstance(row, dict) and len(row.get("document", {}).get("materials", [])) >= 2
        )
    )
    document["issueDate"] = "2026-06-19"
    for card in document["materials"]:
        card["issueDate"] = "2026-06-19"
    for card in document["materials"][:2]:
        card["title"] = "Same historical headline on two different source pages"
        card["publishedAt"] = "2026-06-18T00:00:00Z"
        card["publicationDateStatus"] = "resolved"
    validate_public_issue_document(document)
    document["issueDate"] = "2026-09-19"
    for card in document["materials"]:
        card["issueDate"] = "2026-09-19"
    validate_public_issue_document(document)
