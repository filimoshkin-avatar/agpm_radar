"""Stage 15 post-Legacy comparison and operator-boundary regressions."""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from packages.domain.snapshot import JsonObject
from tools.check_legacy_mirror import FILES
from tools.check_legacy_mirror import main as mirror_main
from tools.find_stage15_catchup import main as catchup_main
from tools.run_stage15_dual import (
    Stage15DualRunError,
    _comparison_verdict,
    _fetch_json,
    _issue_date,
    _llm_status,
    _material_content_differences,
    _next_attempt_root,
    _urls,
)


def test_legacy_and_v2_public_shapes_share_comparison_surface() -> None:
    legacy: JsonObject = {
        "issue": {"issue_date": "2026-08-20"},
        "daily_analysis": {"status": "success"},
        "materials": [
            {"canonical_url": "https://example.test/a"},
            {"url": "https://example.test/b"},
        ],
    }
    v2: JsonObject = {
        "issueDate": "2026-08-20",
        "analysis": {"status": "success"},
        "llm": {"effectiveModel": "gpt-5.5", "status": "success"},
        "materials": [
            {"canonicalUrl": "https://example.test/a"},
            {"url": "https://example.test/b"},
        ],
    }
    assert _issue_date(legacy) == _issue_date(v2) == "2026-08-20"
    assert _urls(legacy) == _urls(v2)
    assert _llm_status(legacy) == _llm_status(v2) == "success"


def test_llm_status_does_not_infer_success_from_nested_shape() -> None:
    assert _llm_status({"analysis": {"analysis": {"summary": "shape drift"}}}) == "unavailable"


def test_failed_attempt_retains_evidence_and_next_attempt_is_available(tmp_path: Path) -> None:
    run_root = tmp_path / "2026-08-21"
    first = _next_attempt_root(run_root)
    (first / "failure.txt").write_text("retained", encoding="utf-8")
    second = _next_attempt_root(run_root)
    assert first.name == "attempt-001"
    assert second.name == "attempt-002"
    assert (first / "failure.txt").read_text(encoding="utf-8") == "retained"


def test_public_fetch_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _bound: int) -> bytes:
            return b'{"issueDate":"2026-08-21","materials":[]}'

    def urlopen(_url: str, timeout: int) -> Response:
        nonlocal calls
        assert timeout == 30
        calls += 1
        if calls < 3:
            raise urllib.error.URLError("not converged")
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    document, _content = _fetch_json("https://example.test/api/issues/2026-08-21")
    assert document["issueDate"] == "2026-08-21"
    assert calls == 3


def test_public_fetch_has_bounded_clear_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(Stage15DualRunError, match="did not converge after 2 attempts"):
        _fetch_json("https://example.test/missing", attempts=2)


def test_comparison_verdict_is_fail_loud_for_unexplained_difference() -> None:
    assert _comparison_verdict(["https://example.test/a"], [], 1)["status"] == "explained"
    verdict = _comparison_verdict(["https://example.test/a"], [], 0)
    assert verdict == {
        "status": "unexplained",
        "alert": True,
        "reason": "url_difference_is_not_explained_by_v2_exclusions",
    }


def test_shared_material_comparison_covers_editorial_content() -> None:
    legacy: JsonObject = {
        "materials": [
            {
                "canonical_url": "https://example.test/a",
                "title": "A",
                "summary": "same",
                "rubrics": ["b", "a"],
            }
        ]
    }
    v2: JsonObject = {
        "materials": [
            {
                "canonicalUrl": "https://example.test/a",
                "title": "A",
                "summary": "same",
                "rubrics": ["a", "b"],
            }
        ]
    }
    assert _material_content_differences(legacy, v2) == []
    changed_v2: JsonObject = {
        "materials": [
            {
                "canonicalUrl": "https://example.test/a",
                "title": "A",
                "summary": "drift",
                "rubrics": ["a", "b"],
            }
        ]
    }
    differences = _material_content_differences(legacy, changed_v2)
    assert differences == [{"fields": ["summary"], "url": "https://example.test/a"}]


def test_catchup_selects_oldest_unfinished_recent_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exports = tmp_path / "exports"
    runs = tmp_path / "runs"
    exports.mkdir()
    (runs / "2026-08-20").mkdir(parents=True)
    (runs / "2026-08-20" / "combined-report.json").write_text("{}", encoding="utf-8")
    for day in ("2026-08-19", "2026-08-20", "2026-08-21"):
        (exports / f"{day}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "find_stage15_catchup.py",
            "--exports-root",
            str(exports),
            "--runs-root",
            str(runs),
            "--through",
            "2026-08-21",
            "--lookback-days",
            "3",
        ],
    )
    assert catchup_main() == 0
    assert capsys.readouterr().out == "2026-08-19\n"


def test_legacy_mirror_fails_on_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = tmp_path / "repository"
    runtime = tmp_path / "runtime"
    repository.mkdir()
    runtime.mkdir()
    for name in FILES:
        (repository / name).write_text(name, encoding="utf-8")
        (runtime / name).write_text(name, encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_legacy_mirror.py",
            "--repository-scripts",
            str(repository),
            "--runtime-scripts",
            str(runtime),
        ],
    )
    assert mirror_main() == 0
    (runtime / FILES[0]).write_text("drift", encoding="utf-8")
    with pytest.raises(SystemExit, match="Legacy runtime mirror drift"):
        mirror_main()


@pytest.mark.parametrize(
    "entrypoint",
    [
        "tools.run_stage15_dual",
        "tools.build_stage14_daily",
        "apps.candidate_builder",
        "apps.migration_runner",
        "apps.publisher_runner",
    ],
)
def test_approved_module_entrypoints_work_from_foreign_cwd(entrypoint: str, tmp_path: Path) -> None:
    v2_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", entrypoint, "--help"],
        cwd=tmp_path,
        env={"PYTHONPATH": str(v2_root)},
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout
