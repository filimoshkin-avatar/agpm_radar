"""Authenticated, version-bound owner composition changes."""

from __future__ import annotations

import base64
import copy
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

from radar_kx.database import Database
from radar_kx.editor_service import EditorService, make_handler
from radar_kx.issue_reviews import ReviewQueue, digest, preview


def fixture() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    issue = {
        "issueDate": "2026-09-19",
        "materials": [
            {"id": "a", "title": "First"},
            {"id": "b", "title": "Second"},
            {"id": "c", "title": "Independent"},
        ],
    }
    report = {
        "reportId": "f" * 64,
        "issueHash": digest(issue),
        "pairs": [
            {
                "pairId": "pair",
                "left": {"materialId": "a", "issueDate": "2026-09-19"},
                "right": {"materialId": "b", "issueDate": "2026-09-19"},
            }
        ],
    }
    payload = {
        "issueDate": issue["issueDate"],
        "reportId": report["reportId"],
        "issueHash": report["issueHash"],
        "labels": {"pair": "same_event"},
        "removeMaterialIds": ["b"],
    }
    return issue, report, payload


def test_preview_and_durable_idempotent_submission(tmp_path: Path) -> None:
    issue, report, payload = fixture()
    result = preview(payload, report, issue)
    assert [card["id"] for card in result["retained"]] == ["a", "c"]
    queue = ReviewQueue(tmp_path)
    job = queue.submit(result, "owner")
    assert job == ReviewQueue(tmp_path).submit(result, "owner")
    assert queue.get(job["reviewId"])["actor"] == "owner"
    queue.update(job["reviewId"], "running", "")
    queue.update(job["reviewId"], "published", "")
    assert queue.get(job["reviewId"])["status"] == "published"
    assert queue.update(job["reviewId"], "running", "")["status"] == "published"
    assert (tmp_path / (job["reviewId"] + ".json")).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "change",
    [
        {"issueHash": "0" * 64},
        {"reportId": "old"},
        {"labels": {}},
        {"labels": {"pair": "different_event"}},
        {"removeMaterialIds": ["a", "b"]},
        {"removeMaterialIds": ["c"]},
        {"removeMaterialIds": ["b", "b"]},
    ],
)
def test_preview_rejects_unconfirmed_stale_or_excessive_removal(change: dict[str, Any]) -> None:
    issue, report, payload = fixture()
    with pytest.raises(ValueError):
        preview({**payload, **change}, report, issue)


def test_historical_card_cannot_be_removed() -> None:
    issue, report, payload = fixture()
    report["pairs"][0]["right"]["issueDate"] = "2026-09-18"
    with pytest.raises(ValueError):
        preview(payload, report, issue)
    payload["removeMaterialIds"] = ["a"]
    assert preview(payload, report, issue)["removeMaterialIds"] == ["a"]
    changed = copy.deepcopy(issue)
    changed["materials"][0]["title"] += "!"
    with pytest.raises(ValueError):
        preview(payload, report, changed)


def test_http_auth_csrf_and_worker_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = EditorService(
        cast(Database, object()),
        token="test-editor-" * 4,
        actor="owner",
        username="test-user",
        password="test-password",
        reviews_directory=tmp_path,
    )
    called: list[Any] = []

    def submit(payload: dict[str, Any], *, submit: bool = False) -> dict[str, Any]:
        called.append((payload, submit))
        return {"status": "queued"}

    monkeypatch.setattr(service, "issue_review", submit)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        basic = "Basic " + base64.b64encode(b"test-user:test-password").decode()
        for headers, status in [
            ({}, 401),
            ({"Authorization": basic}, 403),
            (
                {
                    "Authorization": basic,
                    "Content-Type": "application/json",
                    "Sec-Fetch-Site": "cross-site",
                },
                403,
            ),
            ({"Authorization": basic, "Content-Type": "application/json"}, 200),
        ]:
            client.request("POST", "/api/issue-reviews/submit", "{}", headers)
            response = client.getresponse()
            assert response.status == status
            response.read()
        assert len(called) == 1
        client.request(
            "POST",
            "/api/issue-reviews/status",
            json.dumps({"status": "published"}),
            {"Authorization": basic, "Content-Type": "application/json"},
        )
        response = client.getresponse()
        assert response.status == 403
        response.read()
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_failed_review_can_be_confirmed_again_but_not_beside_active_job(tmp_path: Path) -> None:
    issue, report, payload = fixture()
    queue = ReviewQueue(tmp_path)
    value = preview(payload, report, issue)
    job = queue.submit(value, "owner")
    queue.update(job["reviewId"], "failed", "model unavailable")
    active = queue.submit(value, "second-owner")
    with pytest.raises(ValueError, match="уже выполняется"):
        queue.submit(value, "owner")
    queue.update(active["reviewId"], "failed", "cancelled")
    retried = queue.submit(value, "owner")
    assert retried["status"] == "queued"
    assert retried["retry"] == 1
