"""Daily alerts and content-bound owner removal requests."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from tools.observation_summary import current_summary, notify_result, summarize, summary_lines
from tools.observe_issue_events import _write, analyze
from tools.process_issue_reviews import validate_selection

from test_event_observation import inference, issue


def reviewed(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    document = issue()
    report = analyze(document, [], tmp_path, inference=inference)
    job = {
        "issueDate": report["issueDate"],
        "issueHash": report["issueHash"],
        "reportId": report["reportId"],
        "removeMaterialIds": ["m2"],
        "labels": {report["pairs"][0]["pairId"]: "same_event"},
    }
    return document, report, job


def test_daily_summary_names_suspects_and_does_not_claim_pending_is_clear(tmp_path: Path) -> None:
    document, report, _ = reviewed(tmp_path)
    pending = current_summary(tmp_path, document)
    assert pending["status"] == "pending"
    assert "подозрений нет" not in "\n".join(summary_lines(pending))
    _write(tmp_path / f"{report['issueHash']}.json", report)
    summary = current_summary(tmp_path, document)
    assert summary["suspectedPairs"] == 1
    text = "\n".join(summary_lines(summary))
    assert "Подозрения на повторы: 1" in text
    assert document["materials"][0]["title"] in text
    assert "?issue=2026-09-19#event-observations" in text
    changed = copy.deepcopy(document)
    changed["materials"].pop()
    assert current_summary(tmp_path, changed)["status"] == "pending"


def test_late_result_delivery_retries_failure_and_deduplicates_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, report, _ = reviewed(tmp_path)
    monkeypatch.setenv("RADAR_V2_NOTIFY_CHANNEL", "telegram")
    monkeypatch.setenv("RADAR_V2_NOTIFY_TARGET", "test-owner")
    with patch(
        "tools.observation_summary.subprocess.run",
        side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)],
    ) as run:
        notify_result(tmp_path, report)
        notify_result(tmp_path, report)
        notify_result(tmp_path, report)
    assert run.call_count == 2
    assert len(list((tmp_path / "notifications").glob("*.json"))) == 1
    assert "Подозрения на повторы" in run.call_args.args[0][-1]


def test_review_rejects_stale_or_unconfirmed_removal(tmp_path: Path) -> None:
    document, report, job = reviewed(tmp_path)
    validate_selection(job, report, document)
    invalid_changes: list[dict[str, Any]] = [
        {"issueHash": "0" * 64},
        {"removeMaterialIds": ["m1", "m2"]},
        {"labels": {}},
        {"removeMaterialIds": ["historical"]},
    ]
    for changes in invalid_changes:
        with pytest.raises(ValueError):
            validate_selection({**job, **changes}, report, document)
    document["materials"][0]["title"] += " updated"
    with pytest.raises(ValueError, match="изменился"):
        validate_selection(job, report, document)


def test_failed_check_is_not_reported_as_zero_duplicates(tmp_path: Path) -> None:
    def failed(_: str) -> Any:
        raise TimeoutError

    report = analyze(issue(), [], tmp_path, inference=failed)
    text = "\n".join(summary_lines(summarize(report)))
    assert "ошибки" in text
    assert "подозрений нет" not in text


def test_completed_report_with_missed_notification_is_retried_next_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from tools.observe_issue_events import launch_observation

    _, report, _ = reviewed(tmp_path / "cache")
    root = tmp_path / "event-observations"
    _write(root / (report["issueHash"] + ".json"), report)
    _write(root / "receipts" / (report["reportId"] + ".json"), {"stored": True})
    monkeypatch.setenv("RADAR_V2_NOTIFY_CHANNEL", "telegram")
    monkeypatch.setenv("RADAR_V2_NOTIFY_TARGET", "test-owner")
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
    with patch("tools.observe_issue_events.subprocess.Popen") as launch:
        launch_observation(args)
    assert any("2026-09-19" in call.args[0] for call in launch.call_args_list)


def test_worker_replays_exact_publisher_after_crash_and_only_then_marks_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    import tools.process_issue_reviews as worker

    _, report, job = reviewed(tmp_path / "cache")
    job["reviewId"] = "a" * 64
    args = argparse.Namespace(
        root=tmp_path / "reviews",
        source_root=tmp_path / "source",
        publisher_root=tmp_path / "publisher",
        ssh_host="deploy@example",
        ssh_identity=tmp_path / "key",
    )
    work = args.root / job["reviewId"]
    build = {
        "candidateId": "cand_test",
        "sourceStateHash": "old",
        "package": str(tmp_path / "package"),
        "staging": str(tmp_path / "staging"),
        "applicationReleaseId": "app_release_test",
        "createdAt": "2026-09-19T00:00:00Z",
        "finishedAt": "2026-09-19T00:01:00Z",
    }
    _write(work / "build.json", build)
    _write(args.publisher_root / "requests" / "cand_test.base-pointer.json", {"retained": True})
    calls: list[str] = []

    def publish(*_: Any) -> dict[str, Any]:
        calls.append("source-reconciled")
        return {"status": "already_succeeded"}

    def editor(_: Any, path: str, payload: dict[str, Any]) -> None:
        calls.append(payload["status"])

    monkeypatch.setattr(worker, "editor", editor)
    monkeypatch.setattr(worker, "publish_candidate", publish)
    monkeypatch.setattr(worker, "read_content_pointer", lambda _: SimpleNamespace(state_hash="new"))
    monkeypatch.setattr(worker, "ssh_transport", lambda **_: None)
    monkeypatch.setattr(worker, "launch_observation", lambda _: calls.append("observation"))
    monkeypatch.setattr(
        worker, "_fetch_json", lambda _: pytest.fail("Do not bypass publisher replay")
    )
    worker.process(args, job)
    assert calls == ["running", "source-reconciled", "published", "observation"]
    calls.clear()
    worker.process(args, job)
    assert calls == ["running", "published", "observation"]


def test_correction_builder_translates_regenerated_analysis_to_candidate_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    import sqlite3
    import sys

    import tools.build_stage14_correction as correction
    from packages.contracts.analysis import issue_content_hash
    from packages.domain.candidate_package import verify_candidate_package
    from packages.storage.hashing import logical_state_hash

    from test_stage5_candidate_builder import _seed_database

    source = tmp_path / "source.sqlite"
    _seed_database(source)
    # This historical fixture intentionally has flags that the correction contract
    # does not accept. Normalize only the synthetic source and its retained hash.
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE issue_materials SET flags_json = '[]'")
        connection.row_factory = sqlite3.Row
        extra_id = "material_extra_synthetic01"
        for table in (
            "materials",
            "issue_materials",
            "material_analysis",
            "material_quality",
            "material_rubrics",
        ):
            where = (
                "material_id = ?"
                if table == "materials"
                else "material_id = ? AND issue_id = 'issue_20260819'"
            )
            original = connection.execute(
                f"SELECT * FROM {table} WHERE {where}",  # noqa: S608 -- fixed fixture tables
                ("material_draft_synthetic01",),
            ).fetchone()
            row = dict(original)
            row["material_id"] = extra_id
            if table == "materials":
                row["url"] = "https://example.test/duplicate"
                row["canonical_url"] = row["url"]
            if table == "issue_materials":
                row["sort_order"] = 1
            connection.execute(
                f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",  # noqa: S608 -- fixed fixture tables
                tuple(row.values()),
            )
        connection.execute(
            "UPDATE daily_stats SET included = 2, cut = 40, near = 2, core = 2 WHERE issue_id = 'issue_20260819'"
        )
        state_hash = logical_state_hash(connection)
        connection.execute("UPDATE content_releases SET after_state_hash = ?", (state_hash,))

    def generated(**kwargs: Any) -> dict[str, Any]:
        materials = kwargs["materials"]
        return {
            "signal": "Сигнал после пересмотра.",
            "why_agpm": "Новый управленческий вывод.",
            "watch_next": "Следующий шаг.",
            "theses": [],
            "evidence_material_ids": [materials[0]["materialId"]],
            "evidence_titles": [materials[0]["title"]],
            "input_content_hash": issue_content_hash(materials),
        }

    monkeypatch.setattr(correction, "generate_v2_analysis", generated)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "correction",
            "--source-db",
            str(source),
            "--issue-date",
            "2026-08-19",
            "--candidate-id",
            "cand_owner_regeneration_001",
            "--created-at",
            "2026-09-19T00:00:00Z",
            "--root",
            str(tmp_path / "build"),
            "--regenerate-analysis",
            "--remove-material-id",
            "material_extra_synthetic01",
        ],
    )
    assert correction.main() == 0
    result = json.loads(capsys.readouterr().out)
    package = verify_candidate_package(Path(result["package"]))
    desired: Any = package.candidate["desiredIssue"]
    assert [card["materialId"] for card in desired["materials"]] == ["material_draft_synthetic01"]
    assert desired["stats"]["included"] == 1
    analysis: Any = desired["analysis"]
    assert analysis["blocks"][0]["text"] == "Сигнал после пересмотра."
    assert analysis["evidenceMaterialIds"] == ["material_draft_synthetic01"]
