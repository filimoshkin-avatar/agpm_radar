"""Run the Stage 15 post-Legacy V2 publication and comparison boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
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


def _load_json(path: Path, *, expected_mode: int = 0o644) -> JsonObject:
    value: object = json.loads(read_regular_file(path, expected_mode=expected_mode))
    if not isinstance(value, dict):
        raise Stage15DualRunError(f"JSON root is not an object: {path.name}")
    return cast(JsonObject, value)


def _fetch_json(
    url: str,
    *,
    attempts: int = 5,
    retry_delay_seconds: float = 3.0,
) -> tuple[JsonObject, bytes]:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    content: bytes | None = None
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
                if response.status != 200:
                    raise Stage15DualRunError(f"public endpoint returned HTTP {response.status}")
                content = response.read(8 * 1024 * 1024 + 1)
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt < attempts:
                time.sleep(retry_delay_seconds)
    if content is None:
        raise Stage15DualRunError(
            f"public endpoint did not converge after {attempts} attempts: {last_error}"
        ) from last_error
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


def _material_map(document: JsonObject) -> dict[str, JsonObject]:
    materials = document.get("materials")
    if not isinstance(materials, list):
        raise Stage15DualRunError("materials are absent from public response")
    result: dict[str, JsonObject] = {}
    for raw_item in materials:
        if not isinstance(raw_item, dict):
            raise Stage15DualRunError("material is not an object")
        item = raw_item
        value = item.get("canonical_url") or item.get("canonicalUrl") or item.get("url")
        if not isinstance(value, str):
            raise Stage15DualRunError("material URL is invalid")
        result[value] = item
    return result


def _material_content_differences(legacy: JsonObject, v2: JsonObject) -> list[JsonObject]:
    legacy_materials = _material_map(legacy)
    v2_materials = _material_map(v2)
    fields = (
        ("title", "title"),
        ("summary", "summary"),
        ("agpm_takeaway", "agpmTakeaway"),
        ("brief", "brief"),
        ("theses", "theses"),
        ("trend_notes", "trendNotes"),
        ("perimeter", "perimeter"),
        ("verdict", "verdict"),
        ("signal_strength", "signalStrength"),
        ("key_material", "keyMaterial"),
    )
    differences: list[JsonObject] = []
    for url in sorted(set(legacy_materials) & set(v2_materials)):
        changed = [
            legacy_name
            for legacy_name, v2_name in fields
            if legacy_materials[url].get(legacy_name) != v2_materials[url].get(v2_name)
        ]
        legacy_rubrics = legacy_materials[url].get("rubrics")
        v2_rubrics = v2_materials[url].get("rubrics")
        if isinstance(legacy_rubrics, list) and isinstance(v2_rubrics, list):
            if sorted(map(str, legacy_rubrics)) != sorted(map(str, v2_rubrics)):
                changed.append("rubrics")
        elif legacy_rubrics != v2_rubrics:
            changed.append("rubrics")
        if changed:
            differences.append({"fields": cast(list[JsonValue], changed), "url": url})
    return differences


def _llm_status(document: JsonObject) -> str:
    llm = document.get("llm")
    if isinstance(llm, dict) and isinstance(llm.get("status"), str):
        return cast(str, llm["status"])
    analysis = document.get("daily_analysis") or document.get("analysis")
    if isinstance(analysis, dict):
        status = analysis.get("status")
        if isinstance(status, str):
            return status
    return "unavailable"


def _application_release_id(public_base: str) -> str:
    health, _content = _fetch_json(f"{public_base.rstrip('/')}/api/health")
    value = health.get("applicationReleaseId")
    if not isinstance(value, str) or not value.startswith("app_release_"):
        raise Stage15DualRunError("public health does not identify the active application release")
    return value


def _next_attempt_root(run_root: Path) -> Path:
    run_root.mkdir(mode=0o700, exist_ok=True)
    for number in range(1, 10_000):
        attempt = run_root / f"attempt-{number:03d}"
        try:
            attempt.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return attempt
    raise Stage15DualRunError("daily run exhausted the bounded attempt namespace")


def _excluded_count(build: JsonObject | None) -> int | None:
    if build is None:
        return None
    value = build.get("excludedMaterials")
    return value if isinstance(value, int) and value >= 0 else None


def _retained_excluded_count(run_root: Path) -> int | None:
    candidates = [*sorted(run_root.glob("attempt-*"), reverse=True), run_root]
    for base in candidates:
        path = base / "candidate-build" / "excluded-materials.json"
        if not path.is_file():
            continue
        document = _load_json(path, expected_mode=0o600)
        excluded = document.get("excluded")
        if isinstance(excluded, list):
            return len(excluded)
    return None


def _comparison_verdict(
    legacy_urls: list[str],
    v2_urls: list[str],
    excluded_count: int | None,
    content_differences: list[JsonObject] | None = None,
) -> JsonObject:
    only_legacy = sorted(set(legacy_urls) - set(v2_urls))
    only_v2 = sorted(set(v2_urls) - set(legacy_urls))
    if content_differences:
        return {
            "status": "unexplained",
            "alert": True,
            "reason": "shared_material_content_differs",
        }
    if not only_legacy and not only_v2:
        return {"status": "matched", "alert": False, "reason": "identical_url_sets"}
    if only_v2:
        return {
            "status": "unexplained",
            "alert": True,
            "reason": "v2_published_material_is_absent_from_legacy",
        }
    if excluded_count is None:
        # A replayed or already-published date carries no exclusion evidence of its own.
        # Report the difference without claiming it is unexplained.
        return {
            "status": "unverified",
            "alert": False,
            "reason": "v2_exclusion_evidence_is_unavailable_for_this_date",
        }
    if excluded_count == len(only_legacy):
        return {
            "status": "explained",
            "alert": False,
            "reason": "v2_exclusions_match_only_legacy_count",
        }
    return {
        "status": "unexplained",
        "alert": True,
        "reason": "url_difference_is_not_explained_by_v2_exclusions",
    }


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
    parser.add_argument("--application-release-id")
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
    report_path = run_root / "combined-report.json"
    if report_path.exists():
        report_content = read_regular_file(report_path, expected_mode=0o600)
        print(report_content.decode("utf-8"), end="")
        return 0
    attempt_root = _next_attempt_root(run_root)
    legacy_bytes = read_regular_file(args.legacy_json, expected_mode=0o644)
    snapshot = create_snapshot(
        attempt_root / "snapshots",
        snapshot_id=f"snap_{args.issue_date.replace('-', '')}_stage15",
        collected_at=args.started_at,
        candidates=[legacy],
        safe_evidence_index={"legacyPublicSha256": _sha256(legacy_bytes)},
    )
    fork = fork_snapshot(
        attempt_root / "snapshots" / snapshot.identity.snapshot_id,
        attempt_root / "fork",
        expected_identity=snapshot.identity,
        legacy_consumed_at=args.started_at,
        v2_consumed_at=args.started_at,
    )
    if not fork.both_attest_same_input:
        raise Stage15DualRunError("Legacy and V2 branches did not attest the same snapshot")

    publication: JsonObject | None = None
    build: JsonObject | None = None
    disposition = "already_published"
    if not _source_has_issue(args.source_root, args.issue_date):
        args.application_release_id = args.application_release_id or _application_release_id(
            args.v2_public_base
        )
        build = _build_candidate(args, args.legacy_json, attempt_root)
        publication = _publish(args, build, attempt_root)
        disposition = cast(str, publication["status"])

    v2, v2_bytes = _fetch_json(f"{args.v2_public_base.rstrip('/')}/api/issues/{args.issue_date}")
    if _issue_date(v2) != args.issue_date:
        raise Stage15DualRunError("V2 public issue date differs from requested date")
    pointer = read_content_pointer(args.source_root)
    legacy_urls = _urls(legacy)
    v2_urls = _urls(v2)
    content_differences = _material_content_differences(legacy, v2)
    excluded_count = _excluded_count(build)
    if excluded_count is None:
        excluded_count = _retained_excluded_count(run_root)
    verdict = _comparison_verdict(legacy_urls, v2_urls, excluded_count, content_differences)
    report: JsonObject = {
        "comparisonFormat": "radar-stage15-dual-run/v1",
        "comparisonVerdict": verdict,
        "generatedAt": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
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
        "sharedMaterialDifferences": cast(list[JsonValue], content_differences),
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
    atomic_write_new(report_path, content, mode=0o600)
    print(content.decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
