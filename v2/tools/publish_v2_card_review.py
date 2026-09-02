"""Build and publish one historical V2 card-text correction."""

# ruff: noqa: S603

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.publisher.project_manager import project_manager_report_bytes
from packages.publisher.remote_orchestration import PublishInputs, publish_candidate, ssh_transport
from packages.storage.content_pointer import read_content_pointer
from packages.storage.safe_files import atomic_write_new


def _apply_review(database: Path, review: JsonObject) -> tuple[int, int]:
    """Write the reviewed cards into the projection; cards the review omits keep their text.

    Returns how many cards were updated and how many the issue has beyond them. A card
    outside the issue is an error: the review was built for another issue or another release.
    """
    issue_date = str(review["issueDate"])
    cards = cast(list[dict[str, object]], review["cards"])
    with sqlite3.connect(database) as connection:
        issue = connection.execute(
            "SELECT issue_id FROM issues WHERE issue_date = ?", (issue_date,)
        ).fetchone()
        if issue is None:
            raise ValueError(f"issue is absent: {issue_date}")
        issue_id = str(issue[0])
        expected = {
            str(row[0])
            for row in connection.execute(
                "SELECT material_id FROM issue_materials WHERE issue_id = ?", (issue_id,)
            )
        }
        actual = {str(card["materialId"]) for card in cards}
        if not actual or not actual <= expected:
            raise ValueError(
                f"review material ids are outside the issue: extra={sorted(actual - expected)}"
            )
        updated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for card in cards:
            model = str(card.get("model") or review["model"])
            connection.execute(
                """
                UPDATE material_analysis
                SET short_text = ?, agpm_angle = ?, llm_status = 'success',
                    requested_model = ?, effective_model = ?, provider = 'openai',
                    prompt_version = ?, updated_at = ?
                WHERE issue_id = ? AND material_id = ?
                """,
                (
                    card["shortText"],
                    card["agpmAngle"],
                    model,
                    model,
                    review["promptVersion"],
                    updated_at,
                    issue_id,
                    card["materialId"],
                ),
            )
        connection.commit()
    return len(actual), len(expected - actual)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--publisher-root", required=True, type=Path)
    parser.add_argument("--application-release-id", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-identity", required=True, type=Path)
    parser.add_argument(
        "--revision",
        type=int,
        default=1,
        help="Number in the candidate id; raise it when the issue already had a card review.",
    )
    args = parser.parse_args()

    review = cast(JsonObject, json.loads(args.review.read_bytes()))
    issue_date = str(review["issueDate"])
    stamp = issue_date.replace("-", "")
    candidate_id = f"cand_correct_{stamp}_card_review_v{args.revision}"
    args.root.mkdir(mode=0o700, parents=True, exist_ok=False)
    pointer = read_content_pointer(args.source_root)
    projection = args.root / "projection.sqlite"
    shutil.copy2(pointer.database_path, projection)
    updated, untouched = _apply_review(projection, review)
    print(
        f"{candidate_id}: {updated} card(s) updated, {untouched} left as published", file=sys.stderr
    )

    now = datetime.now(UTC).replace(microsecond=0)
    created_at = now.isoformat().replace("+00:00", "Z")
    build_root = args.root / "build"
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
            str(build_root),
            "--llm-success-model",
            str(review["model"]),
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
