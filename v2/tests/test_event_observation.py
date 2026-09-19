"""Observe-only publication, hypothesis evidence, isolation and calibration regressions."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from packages.contracts.event_dedup import EventDedupError, production_gate
from packages.contracts.event_observation import digest, issue_hash, validate_report
from packages.publisher.event_observation import install_observation
from packages.storage.event_observations import read_observation
from packages.storage.mutation_lock import acquire_mutation_lock, release_mutation_lock
from packages.validation.public_issue import build_public_issue
from tools.observe_issue_events import (
    PROMPT,
    _write,
    analyze,
    candidate_pairs,
    card_record,
    import_labels,
    launch_observation,
)

from test_stage5_candidate_builder import _seed_database


def issue() -> dict[str, Any]:
    return {
        "issueDate": "2026-09-19",
        "materials": [
            {
                "id": "m1",
                "title": "Anthropic launches Claude",
                "url": "https://a.example/news",
                "summary": "Anthropic launched Claude for advisors on September 14.",
            },
            {
                "id": "m2",
                "title": "Claude arrives for advisors",
                "url": "https://b.example/news",
                "summary": "Anthropic launched Claude for advisors on September 14. Partners include Schwab.",
            },
        ],
    }


def inference(prompt: str) -> object:
    pairs = json.loads(prompt.removeprefix(PROMPT + "\n"))
    return {
        "predictions": [
            {
                "pairId": pair["pairId"],
                "relation": "same_event",
                "confidence": 0.97,
                "commonEvent": "Claude launch",
                "uniqueFactsLeft": [],
                "uniqueFactsRight": ["Schwab"],
                "explanation": "Two articles describe the launch.",
                "evidenceLeft": "Anthropic",
                "evidenceRight": "Anthropic",
            }
            for pair in pairs
        ]
    }


def test_same_event_report_preserves_cards_and_accepts_missing_passports(tmp_path: Path) -> None:
    document = issue()
    original = copy.deepcopy(document)
    production_gate(document["materials"], require_events=True)
    report = analyze(document, [], tmp_path, inference=inference)
    assert document == original
    assert report["status"] == "complete"
    assert report["pairs"][0]["prediction"]["relation"] == "same_event"
    assert report["coverage"] == {
        "currentCards": 2,
        "historicalCards": 0,
        "candidatePairs": 1,
        "checkedPairs": 1,
    }
    production_gate(document["materials"], require_events=True)


def test_exact_identity_still_blocks() -> None:
    cards = issue()["materials"]
    with pytest.raises(EventDedupError, match="material_id"):
        production_gate([cards[0], cards[0]])
    with pytest.raises(EventDedupError, match="normalized_url"):
        production_gate([cards[0], {**cards[1], "url": cards[0]["url"] + "?utm_source=copy"}])
    production_gate([{**row, "eventDedup": {"invalid": "passport"}} for row in cards])


def test_timeout_and_invented_evidence_are_errors_not_no_duplicates(tmp_path: Path) -> None:
    def timeout(_prompt: str) -> object:
        raise TimeoutError

    document = issue()
    assert analyze(document, [], tmp_path, inference=timeout)["status"] == "error"

    def invented(prompt: str) -> object:
        result: Any = inference(prompt)
        result["predictions"][0]["evidenceLeft"] = "Unpublished quotation"
        return result

    assert analyze(document, [], tmp_path, inference=invented)["status"] == "error"
    assert not list(tmp_path.glob("*.json"))


def test_cache_uses_exact_focus_and_model_prompt(tmp_path: Path) -> None:
    document = issue()
    analyze(document, [], tmp_path, inference=inference)
    with patch("tools.observe_issue_events.infer", side_effect=AssertionError) as model:
        assert analyze(document, [], tmp_path, inference=model)["status"] == "complete"
        model.assert_not_called()
    changed = copy.deepcopy(document)
    changed["materials"][1]["summary"] += " New result."
    with patch("tools.observe_issue_events.infer", side_effect=TimeoutError) as model:
        assert analyze(changed, [], tmp_path, inference=model)["status"] == "error"
        model.assert_called_once()


def test_partial_is_not_complete_and_replay_can_finish(tmp_path: Path) -> None:
    document = issue()
    historical = [
        card_record({**row, "id": "old-" + row["id"]}, "2026-09-15")
        for row in document["materials"]
    ]
    with (
        patch("tools.observe_issue_events.BATCH_SIZE", 1),
        patch("tools.observe_issue_events.MAX_CALLS", 1),
    ):
        report = analyze(document, historical, tmp_path, inference=inference)
    assert report["status"] == "partial"
    assert report["coverage"]["checkedPairs"] < report["coverage"]["candidatePairs"]
    bad = {**report, "status": "complete"}
    bad.pop("reportId")
    bad["reportId"] = digest(bad)
    with pytest.raises(ValueError, match="incomplete"):
        validate_report(bad)
    with patch("tools.observe_issue_events.BATCH_SIZE", 1):
        assert analyze(document, historical, tmp_path, inference=inference)["status"] == "complete"


def test_stale_report_never_applies_to_corrected_issue(tmp_path: Path) -> None:
    document = issue()
    report = analyze(document, [], tmp_path / "cache", inference=inference)
    _write(tmp_path / f"{issue_hash(document)}.json", report)
    assert read_observation(tmp_path, document)["status"] == "complete"
    changed = copy.deepcopy(document)
    changed["materials"][0]["summary"] = "Different event"
    assert read_observation(tmp_path, changed)["status"] != "complete"
    _write(tmp_path / f"{issue_hash(changed)}.json", report)
    assert read_observation(tmp_path, changed)["status"] == "error"


def test_labels_are_separate_append_only_and_bound_to_report(tmp_path: Path) -> None:
    report = analyze(issue(), [], tmp_path / "cache", inference=inference)
    report_path = tmp_path / "reports" / f"{report['reportId']}.json"
    _write(report_path, report)
    original = report_path.read_bytes()
    labels = {
        "reportId": report["reportId"],
        "labels": {report["pairs"][0]["pairId"]: "different_event"},
    }
    path = tmp_path / "labels-export.json"
    _write(path, labels)
    assert import_labels(path, tmp_path, "editor") == 1
    assert report_path.read_bytes() == original
    assert len(list((tmp_path / "labels").glob("*.json"))) == 1
    labels["labels"] = {"wrong-pair": "same_event"}
    _write(path, labels)
    with pytest.raises(ValueError, match="this report"):
        import_labels(path, tmp_path, "editor")


def test_launch_failure_never_raises_on_publication_path(tmp_path: Path) -> None:
    args = argparse.Namespace(
        runs_root=tmp_path,
        python="python",
        issue_date="2026-09-19",
        source_root=tmp_path,
        v2_public_base="https://radar.example",
        ssh_host="deploy@example",
        ssh_identity=tmp_path / "identity",
        v2_root=tmp_path,
    )
    with patch("tools.observe_issue_events.subprocess.Popen", side_effect=OSError):
        launch_observation(args)


def test_effective_focus_uses_reader_text_not_hidden_review_background() -> None:
    material = {
        **issue()["materials"][0],
        "llm": {"status": "success"},
        "llmShortText": "New independent Dispatch event",
        "llmAgpmAngle": "Visible angle",
        "summary": "Old Claude launch",
        "brief": "Old hidden lead",
    }
    focus = card_record(material, "2026-09-19")["focus"]
    assert "New independent Dispatch event" in focus and "Visible angle" in focus
    assert "Old hidden lead" not in focus and "Old Claude launch" not in focus
    material["llm"] = {"status": "fallback"}
    assert "Old hidden lead" in card_record(material, "2026-09-19")["focus"]


def test_install_uses_separate_lock_and_never_changes_database(tmp_path: Path) -> None:
    database = tmp_path / "source.sqlite"
    _seed_database(database)
    with sqlite3.connect(database) as connection:
        document = build_public_issue(connection, issue_date="2026-08-19")
    report = analyze(document, [], tmp_path / "cache", inference=inference)
    original = database.read_bytes()
    mutation = tmp_path / "mutation"
    mutation.mkdir(mode=0o700)
    lock = acquire_mutation_lock(mutation)
    try:
        with patch(
            "packages.publisher.event_observation.read_content_pointer",
            return_value=SimpleNamespace(database_path=database),
        ):
            result = install_observation(
                report,
                content_root=tmp_path,
                root=tmp_path / "observations",
                api_uid=os.getuid(),
                api_gid=os.getgid(),
            )
            assert result["reportId"] == report["reportId"]
            assert database.read_bytes() == original
            assert read_observation(tmp_path / "observations", document)["status"] == "complete"
            bad = {**report, "issueHash": "0" * 64}
            bad.pop("reportId")
            bad["reportId"] = digest(bad)
            with pytest.raises(ValueError, match="different published issue"):
                install_observation(
                    bad,
                    content_root=tmp_path,
                    root=tmp_path / "observations",
                    api_uid=os.getuid(),
                    api_gid=os.getgid(),
                )
    finally:
        release_mutation_lock(lock)


def test_daily_launch_recovers_prior_partial_report(tmp_path: Path) -> None:
    root = tmp_path / "event-observations"
    with patch("tools.observe_issue_events.MAX_CALLS", 0):
        report = analyze(issue(), [], tmp_path / "cache", inference=inference)
    _write(root / f"{report['issueHash']}.json", report)
    args = argparse.Namespace(
        runs_root=tmp_path,
        python="python",
        issue_date="2026-09-20",
        source_root=tmp_path,
        v2_public_base="https://radar.example",
        ssh_host="deploy@example",
        ssh_identity=tmp_path / "identity",
        v2_root=tmp_path,
    )
    with patch("tools.observe_issue_events.subprocess.Popen") as spawn:
        launch_observation(args)
    assert spawn.call_count == 2
    assert spawn.call_args_list[0].args[0][4] == "2026-09-20"
    assert spawn.call_args_list[1].args[0][4] == "2026-09-19"


def test_named_product_retrieves_history_despite_different_language() -> None:
    current = card_record(
        {
            "id": "current",
            "title": "Ant International запускает платформу",
            "url": "https://new.example",
            "summary": "Новая финансовая платформа",
        },
        "2026-09-19",
    )
    old = card_record(
        {
            "id": "old",
            "title": "Ant brings digital identity",
            "url": "https://old.example",
            "summary": "KYA identity verification was introduced",
        },
        "2026-09-10",
    )
    pairs = candidate_pairs([current], [old])
    assert len(pairs) == 1
    assert pairs[0]["right"]["materialId"] == "old"


def test_daily_recovers_job_that_failed_before_public_fetch(tmp_path: Path) -> None:
    root = tmp_path / "event-observations"
    _write(root / "jobs" / "2026-09-19.json", {"issueDate": "2026-09-19", "pending": True})
    args = argparse.Namespace(
        runs_root=tmp_path,
        python="python",
        issue_date="2026-09-20",
        source_root=tmp_path,
        v2_public_base="https://radar.example",
        ssh_host="deploy@example",
        ssh_identity=tmp_path / "identity",
        v2_root=tmp_path,
    )
    with patch("tools.observe_issue_events.subprocess.Popen") as spawn:
        launch_observation(args)
    assert spawn.call_count == 2
    assert spawn.call_args_list[1].args[0][4] == "2026-09-19"
