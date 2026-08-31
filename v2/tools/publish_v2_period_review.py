"""Apply and publish one V2 period-analysis correction."""

# ruff: noqa: S603

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.publisher.project_manager import project_manager_report_bytes
from packages.publisher.remote_orchestration import PublishInputs, publish_candidate, ssh_transport
from packages.storage.content_pointer import read_content_pointer
from packages.storage.safe_files import atomic_write_new

from tools.v2_period_analysis import period_blocks, strip_period_blocks


def _apply(database: Path, review: JsonObject) -> None:
    issue_date = str(review["issueDate"])
    periods = cast(dict[str, JsonObject], review["periods"])
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT issue_id FROM issues WHERE issue_date = ?", (issue_date,)
        ).fetchone()
        if row is None:
            raise ValueError(f"issue is absent: {issue_date}")
        issue_id = str(row[0])
        analysis_row = connection.execute(
            "SELECT analysis_json FROM issue_analysis WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        if analysis_row is None:
            raise ValueError(f"issue analysis is absent: {issue_date}")
        analysis = json.loads(str(analysis_row[0]))
        blocks = cast(list[JsonObject], analysis.get("blocks") or [])
        analysis["blocks"] = strip_period_blocks(blocks) + period_blocks(periods)
        connection.execute(
            "UPDATE issue_analysis SET analysis_json = ?, updated_at = ? WHERE issue_id = ?",
            (
                json.dumps(analysis, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                issue_id,
            ),
        )
        connection.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--publisher-root", required=True, type=Path)
    parser.add_argument("--application-release-id", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-identity", required=True, type=Path)
    args = parser.parse_args()
    review = cast(JsonObject, json.loads(args.review.read_bytes()))
    issue_date = str(review["issueDate"])
    stamp = issue_date.replace("-", "")
    candidate_id = f"cand_correct_{stamp}_period_analysis_v1"
    args.root.mkdir(mode=0o700, parents=True, exist_ok=False)
    pointer = read_content_pointer(args.source_root)
    projection = args.root / "projection.sqlite"
    shutil.copy2(pointer.database_path, projection)
    _apply(projection, review)
    now = datetime.now(UTC).replace(microsecond=0)
    created_at = now.isoformat().replace("+00:00", "Z")
    completed = subprocess.run(
        [
            str(Path(__file__).parents[1] / ".venv" / "bin" / "python"),
            "-m",
            "tools.build_stage14_correction",
            "--source-db",
            str(pointer.database_path),
            "--projection-db",
            str(projection),
            "--issue-date",
            issue_date,
            "--candidate-id",
            candidate_id,
            "--created-at",
            created_at,
            "--root",
            str(args.root / "build"),
            "--llm-success-model",
            "openai/gpt-5.5",
            "--llm-success-provider",
            "openai",
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr[-4000:])
    build = cast(JsonObject, json.loads(completed.stdout))
    result = publish_candidate(
        PublishInputs(
            package=Path(str(build["package"])),
            candidate_staging=Path(str(build["staging"])),
            source_root=args.source_root,
            work_root=args.publisher_root,
            application_release_id=args.application_release_id,
            created_at=created_at,
            finished_at=(now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            duration_ms=60_000,
        ),
        ssh_transport(host=args.ssh_host, identity=args.ssh_identity),
    )
    atomic_write_new(args.root / "publisher-result.json", canonical_json_line(result), mode=0o600)
    atomic_write_new(
        args.root / "project-manager-report.json", project_manager_report_bytes(result), mode=0o600
    )
    print(canonical_json_line(result).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
