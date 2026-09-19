"""Process owner-confirmed composition corrections through the ordinary V2 publisher.

Run on the control host every two minutes. SSH uses the fixed authenticated
editor bridge; neither a browser nor the editor receives publisher credentials.
"""

# ruff: noqa: S603,S607,RUF001
from __future__ import annotations

import argparse
import fcntl
import json
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.contracts.event_observation import issue_hash, validate_report
from packages.publisher.remote_orchestration import PublishInputs, publish_candidate, ssh_transport
from packages.storage.content_pointer import read_content_pointer
from packages.validation.public_issue import build_public_issue

from tools.generate_v2_analysis import WORST_CASE_SECONDS
from tools.observe_issue_events import _write, launch_observation
from tools.run_stage15_dual import _application_release_id, _fetch_json


def editor(args: argparse.Namespace, path: str, payload: dict[str, Any] | None = None) -> Any:
    result = subprocess.run(
        [
            "ssh",
            "-i",
            str(args.editor_identity),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            args.editor_host,
            "python3 /opt/radar-kx/current/scripts/issue_review_bridge.py",
        ],
        input=json.dumps({"path": path, "payload": payload}),
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("editor bridge unavailable")
    return json.loads(result.stdout)


def validate_selection(job: dict[str, Any], report: dict[str, Any], issue: dict[str, Any]) -> None:
    validate_report(report)
    if (
        job["reportId"] != report["reportId"]
        or job["issueHash"] != report["issueHash"]
        or job["issueHash"] != issue_hash(issue)
        or job["issueDate"] != issue["issueDate"]
    ):
        raise ValueError("Выпуск изменился после оценки; требуется новый пересмотр.")
    pairs = {p["pairId"]: p for p in report["pairs"]}
    allowed = {
        card["materialId"]
        for key, label in job["labels"].items()
        if label == "same_event" and key in pairs
        for card in (pairs[key]["left"], pairs[key]["right"])
        if card["issueDate"] == job["issueDate"]
    }
    removed = set(job["removeMaterialIds"])
    current = {card["id"] for card in issue["materials"]}
    if not removed or not removed <= allowed or not removed < current:
        raise ValueError("Состав не соответствует редакторской оценке.")
    for key, label in job["labels"].items():
        if (
            label == "same_event"
            and key in pairs
            and all(
                card["issueDate"] == job["issueDate"] and card["materialId"] in removed
                for card in (pairs[key]["left"], pairs[key]["right"])
            )
        ):
            raise ValueError("Нельзя исключить обе карточки подтверждённого события.")


def process(args: argparse.Namespace, job: dict[str, Any]) -> None:
    key = job["reviewId"]
    # Validate before using any remote identifier as a local path.
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("invalid review id")
    if (
        not isinstance(job.get("reportId"), str)
        or re.fullmatch(r"[0-9a-f]{64}", job["reportId"]) is None
    ):
        raise ValueError("invalid report id")
    if (
        not isinstance(job.get("issueDate"), str)
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", job["issueDate"]) is None
    ):
        raise ValueError("invalid issue date")
    work = args.root / key
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    editor(args, "/api/issue-reviews/status", {"reviewId": key, "status": "running"})
    result_path = work / "result.json"
    build_path = work / "build.json"
    if not result_path.exists():
        build = json.loads(build_path.read_bytes()) if build_path.exists() else None
        pointer = read_content_pointer(args.source_root)
        if build is not None:
            # A base-pointer receipt precedes any publisher transport. Once it exists,
            # always replay that exact package, including source-pointer crash recovery.
            request_started = (
                args.publisher_root / "requests" / (build["candidateId"] + ".base-pointer.json")
            )
            if not request_started.exists() and build["sourceStateHash"] != pointer.state_hash:
                build = None
        if build is None:
            public, _ = _fetch_json(f"{args.v2_public_base}/api/issues/{job['issueDate']}")
            report = validate_report(
                json.loads(
                    (
                        args.runs_root
                        / "event-observations"
                        / "reports"
                        / (job["reportId"] + ".json")
                    ).read_bytes()
                )
            )
            validate_selection(job, report, public)
            with sqlite3.connect(pointer.database_path) as connection:
                local = build_public_issue(connection, issue_date=job["issueDate"])
            if issue_hash(local) != job["issueHash"]:
                raise ValueError("Локальная и опубликованная версии выпуска расходятся.")
            attempt = len([path for path in work.glob("build-*") if path.is_dir()]) + 1
            if attempt > 3 * (job.get("retry", 0) + 1):
                raise ValueError(
                    "Пересборка не удалась после трёх попыток. Можно повторить подтверждение."
                )
            now = datetime.now(UTC).replace(microsecond=0)
            created = now.isoformat().replace("+00:00", "Z")
            candidate_id = (
                "cand_correct_"
                + job["issueDate"].replace("-", "")
                + "_"
                + key[:16]
                + "_"
                + str(attempt)
            )
            build_root = work / f"build-{attempt:03d}"
            command = [
                str(args.python),
                "-m",
                "tools.build_stage14_correction",
                "--regenerate-analysis",
                "--source-db",
                str(pointer.database_path),
                "--issue-date",
                job["issueDate"],
                "--candidate-id",
                candidate_id,
                "--created-at",
                created,
                "--root",
                str(build_root),
            ]
            for material_id in job["removeMaterialIds"]:
                command.extend(["--remove-material-id", material_id])
            built = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=WORST_CASE_SECONDS + 120,
                cwd=args.v2_root,
            )
            if built.returncode:
                _write(work / f"build-error-{attempt:03d}.json", {"stderr": built.stderr[-12000:]})
                raise RuntimeError("Пересборка временно не удалась; повторим автоматически.")
            build = json.loads(built.stdout)
            build.update(
                createdAt=created,
                finishedAt=(now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                applicationReleaseId=_application_release_id(args.v2_public_base),
                sourceStateHash=pointer.state_hash,
                candidateId=candidate_id,
            )
            _write(build_path, build)
        result = publish_candidate(
            PublishInputs(
                package=Path(build["package"]),
                candidate_staging=Path(build["staging"]),
                source_root=args.source_root,
                work_root=args.publisher_root,
                application_release_id=build["applicationReleaseId"],
                created_at=build["createdAt"],
                finished_at=build["finishedAt"],
                duration_ms=60000,
            ),
            ssh_transport(host=args.ssh_host, identity=args.ssh_identity),
        )
        _write(result_path, result)
    result = json.loads(result_path.read_bytes())
    if result.get("status") not in {"published", "already_succeeded"}:
        raise RuntimeError("Публикация требует проверки оператором; результат сохранён.")
    editor(args, "/api/issue-reviews/status", {"reviewId": key, "status": "published"})
    args.issue_date = job["issueDate"]
    launch_observation(args)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--publisher-root", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--editor-host", default="root@radar.agpm.space")
    parser.add_argument("--editor-identity", type=Path, default=Path("/root/.ssh/local_ru_admin"))
    parser.add_argument("--ssh-host", default="radar-v2-deploy@radar.agpm.space")
    parser.add_argument(
        "--ssh-identity", type=Path, default=Path("/root/.ssh/radar_v2_publisher_stage13")
    )
    parser.add_argument("--v2-public-base", default="https://radar.agpm.space")
    parser.add_argument("--v2-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    args.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (args.root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        for job in editor(args, "/api/issue-reviews")["reviews"]:
            if job["status"] not in {"queued", "running"}:
                continue
            try:
                process(args, job)
            except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                detail = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "Нужна проверка оператором. Журнал коррекции сохранён."
                )
                editor(
                    args,
                    "/api/issue-reviews/status",
                    {
                        "reviewId": job["reviewId"],
                        "status": "failed" if isinstance(error, ValueError) else "running",
                        "detail": detail[:1000],
                    },
                )
                print(f"review {job['reviewId']}: {type(error).__name__}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
