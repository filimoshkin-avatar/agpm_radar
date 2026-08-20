"""Run the Stage 15 post-Legacy V2 publication and comparison boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import urllib.request
from datetime import date
from pathlib import Path
from typing import cast

from packages.contracts.json_types import JsonValue
from packages.domain.dual_run import fork_snapshot
from packages.domain.snapshot import JsonObject, canonical_json_line, create_snapshot
from packages.publisher.project_manager import project_manager_report_bytes
from packages.publisher.remote_orchestration import PublishInputs, publish_candidate, ssh_transport
from packages.storage.content_pointer import read_content_pointer
from packages.storage.safe_files import (
    atomic_write_new,
    ensure_private_directory,
    read_regular_file,
)


class Stage15DualRunError(RuntimeError):
    """The isolated V2 branch could not produce a proved comparison."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_json(path: Path) -> JsonObject:
    value: object = json.loads(read_regular_file(path, expected_mode=0o644))
    if not isinstance(value, dict):
        raise Stage15DualRunError(f"JSON root is not an object: {path.name}")
    return cast(JsonObject, value)


def _fetch_json(url: str) -> tuple[JsonObject, bytes]:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        if response.status != 200:
            raise Stage15DualRunError(f"public endpoint returned HTTP {response.status}")
        content = response.read(8 * 1024 * 1024 + 1)
    if len(content) > 8 * 1024 * 1024:
        raise Stage15DualRunError("public response exceeds the 8 MiB bound")
    value: object = json.loads(content)
    if not isinstance(value, dict):
        raise Stage15DualRunError("public response is not an object")
    return cast(JsonObject, value), content


def _issue_date(document: JsonObject) -> str:
    issue = document.get("issue")
    if isinstance(issue, dict) and isinstance(issue.get("issue_date"), str):
        return cast(str, issue["issue_date"])
    value = document.get("issueDate")
    if isinstance(value, str):
        return value
    raise Stage15DualRunError("issue date is absent from public response")


def _urls(document: JsonObject) -> list[str]:
    materials = document.get("materials")
    if not isinstance(materials, list):
        raise Stage15DualRunError("materials are absent from public response")
    result: list[str] = []
    for item in materials:
        if not isinstance(item, dict):
            raise Stage15DualRunError("material is not an object")
        value = item.get("canonical_url") or item.get("canonicalUrl") or item.get("url")
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            raise Stage15DualRunError("material URL is invalid")
        result.append(value)
    if len(set(result)) != len(result):
        raise Stage15DualRunError("public response repeats a material URL")
    return sorted(result)


def _llm_status(document: JsonObject) -> str:
    llm = document.get("llm")
    if isinstance(llm, dict) and isinstance(llm.get("status"), str):
        return cast(str, llm["status"])
    analysis = document.get("daily_analysis") or document.get("analysis")
    if isinstance(analysis, dict):
        nested = analysis.get("analysis")
        status = analysis.get("status")
        if isinstance(status, str):
            return status
        if isinstance(nested, dict):
            return "success"
    return "unavailable"


def _source_has_issue(source_root: Path, issue_date: str) -> bool:
    pointer = read_content_pointer(source_root)
    with sqlite3.connect(f"file:{pointer.database_path}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        return (
            connection.execute(
                "SELECT 1 FROM issues WHERE issue_date = ? AND lifecycle_status = 'published'",
                (issue_date,),
            ).fetchone()
            is not None
        )


def _build_candidate(args: argparse.Namespace, legacy_json: Path, run_root: Path) -> JsonObject:
    pointer = read_content_pointer(args.source_root)
    candidate_id = f"cand_stage15_daily_{args.issue_date.replace('-', '')}_01"
    command = [
        str(args.python),
        "-m",
        "tools.build_stage14_daily",
        "--legacy-json",
        str(legacy_json),
        "--legacy-db",
        str(args.legacy_db),
        "--source-db",
        str(pointer.database_path),
        "--candidate-id",
        candidate_id,
        "--created-at",
        args.started_at,
        "--published-at",
        args.started_at,
        "--root",
        str(run_root / "candidate-build"),
    ]
    completed = subprocess.run(  # noqa: S603
        command,
        cwd=args.v2_root,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace")[-4000:]
        raise Stage15DualRunError(f"candidate build failed ({completed.returncode}): {message}")
    value: object = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise Stage15DualRunError("candidate builder output is not an object")
    return cast(JsonObject, value)


def _publish(args: argparse.Namespace, build: JsonObject, run_root: Path) -> JsonObject:
    package = Path(cast(str, build["package"]))
    staging = Path(cast(str, build["staging"]))
    result = publish_candidate(
        PublishInputs(
            package=package,
            candidate_staging=staging,
            source_root=args.source_root,
            work_root=args.publisher_root,
            application_release_id=args.application_release_id,
            created_at=args.started_at,
            finished_at=args.finished_at,
            duration_ms=args.duration_ms,
        ),
        ssh_transport(host=args.ssh_host, identity=args.ssh_identity),
    )
    atomic_write_new(run_root / "publisher-result.json", canonical_json_line(result), mode=0o600)
    atomic_write_new(
        run_root / "project-manager-report.json",
        project_manager_report_bytes(result),
        mode=0o600,
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-date", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--finished-at", required=True)
    parser.add_argument("--duration-ms", type=int, default=60_000)
    parser.add_argument("--legacy-json", required=True, type=Path)
    parser.add_argument("--legacy-db", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--publisher-root", required=True, type=Path)
    parser.add_argument("--runs-root", required=True, type=Path)
    parser.add_argument("--v2-root", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-identity", required=True, type=Path)
    parser.add_argument("--application-release-id", required=True)
    parser.add_argument("--v2-public-base", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    date.fromisoformat(args.issue_date)
    legacy = _load_json(args.legacy_json)
    if _issue_date(legacy) != args.issue_date:
        raise Stage15DualRunError("Legacy output is not ready for the requested issue date")
    ensure_private_directory(args.runs_root)
    run_root = args.runs_root / args.issue_date
    if run_root.exists():
        report_content = read_regular_file(run_root / "combined-report.json", expected_mode=0o600)
        print(report_content.decode("utf-8"), end="")
        return 0
    run_root.mkdir(mode=0o700)
    legacy_bytes = read_regular_file(args.legacy_json, expected_mode=0o644)
    snapshot = create_snapshot(
        run_root / "snapshots",
        snapshot_id=f"snap_{args.issue_date.replace('-', '')}_stage15",
        collected_at=args.started_at,
        candidates=[legacy],
        safe_evidence_index={"legacyPublicSha256": _sha256(legacy_bytes)},
    )
    fork = fork_snapshot(
        run_root / "snapshots" / snapshot.identity.snapshot_id,
        run_root / "fork",
        expected_identity=snapshot.identity,
        legacy_consumed_at=args.started_at,
        v2_consumed_at=args.started_at,
    )
    if not fork.both_attest_same_input:
        raise Stage15DualRunError("Legacy and V2 branches did not attest the same snapshot")

    publication: JsonObject | None = None
    disposition = "already_published"
    if not _source_has_issue(args.source_root, args.issue_date):
        publication = _publish(args, _build_candidate(args, args.legacy_json, run_root), run_root)
        disposition = cast(str, publication["status"])

    v2, v2_bytes = _fetch_json(f"{args.v2_public_base.rstrip('/')}/api/issues/{args.issue_date}")
    if _issue_date(v2) != args.issue_date:
        raise Stage15DualRunError("V2 public issue date differs from requested date")
    pointer = read_content_pointer(args.source_root)
    legacy_urls = _urls(legacy)
    v2_urls = _urls(v2)
    report: JsonObject = {
        "comparisonFormat": "radar-stage15-dual-run/v1",
        "generatedAt": args.finished_at,
        "issueDate": args.issue_date,
        "legacy": {
            "llmStatus": _llm_status(legacy),
            "materialCount": len(legacy_urls),
            "publicSha256": _sha256(legacy_bytes),
            "status": "published",
        },
        "publication": {
            "disposition": disposition,
            "result": publication,
        },
        "snapshot": {
            "itemCount": snapshot.identity.item_count,
            "manifestSha256": snapshot.identity.manifest_sha256,
            "payloadSha256": snapshot.identity.payload_sha256,
            "snapshotId": snapshot.identity.snapshot_id,
        },
        "urlDifferences": {
            "onlyLegacy": cast(list[JsonValue], sorted(set(legacy_urls) - set(v2_urls))),
            "onlyV2": cast(list[JsonValue], sorted(set(v2_urls) - set(legacy_urls))),
        },
        "v2": {
            "llmStatus": _llm_status(v2),
            "materialCount": len(v2_urls),
            "publicSha256": _sha256(v2_bytes),
            "releaseId": pointer.release_id,
            "stateHash": pointer.state_hash,
            "status": "published",
        },
    }
    content = canonical_json_line(report)
    atomic_write_new(run_root / "combined-report.json", content, mode=0o600)
    print(content.decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
