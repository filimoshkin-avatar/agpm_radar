from __future__ import annotations

import threading
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from radar_kx.config import Settings
from radar_kx.fetcher import DocumentTask
from radar_kx.worker import run_until_idle


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        dsn="",
        release_id="test",
        capacity_path=str(tmp_path),
        user_agent="Radar-KX-Test/1.0",
        request_timeout_seconds=2,
        connect_timeout_seconds=2,
        per_host_interval_seconds=0.001,
        max_body_bytes=100_000,
        min_text_chars=30,
        min_free_bytes=1,
        lease_seconds=60,
        max_attempts=2,
        max_in_flight_per_host=8,
        respect_robots=False,
    )


def test_worker_replenishes_a_free_slot_before_slow_task_finishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tasks = deque(
        DocumentTask(name, f"https://example.com/{name}", 1, None, None)
        for name in ("slow", "fast", "third")
    )
    release_slow = threading.Event()
    slow_completed = threading.Event()
    third_started_before_slow_completed: list[bool] = []

    class FakeDatabase:
        def __init__(self, _settings_value: Settings) -> None:
            self.recorded: list[str] = []

        def claim_tasks(self, *, limit: int, per_host_limit: int) -> list[DocumentTask]:
            assert per_host_limit == 8
            return [tasks.popleft() for _ in range(min(limit, len(tasks)))]

        def record_fetch_result(self, result: DocumentTask) -> dict[str, Any]:
            self.recorded.append(result.document_id)
            return {"documentId": result.document_id, "status": "succeeded"}

        def status(self) -> dict[str, Any]:
            return {"recorded": len(self.recorded)}

    def fake_fetch_document(*, task: DocumentTask, **_kwargs: object) -> DocumentTask:
        if task.document_id == "slow":
            assert release_slow.wait(2)
            slow_completed.set()
        elif task.document_id == "third":
            third_started_before_slow_completed.append(not slow_completed.is_set())
            release_slow.set()
        return task

    monkeypatch.setattr("radar_kx.worker.Database", FakeDatabase)
    monkeypatch.setattr("radar_kx.worker.fetch_document", fake_fetch_document)
    monkeypatch.setattr("radar_kx.worker._log", lambda *_args, **_kwargs: None)

    result = run_until_idle(_settings(tmp_path), workers=2)

    assert result["processed"] == 3
    assert third_started_before_slow_completed == [True]
