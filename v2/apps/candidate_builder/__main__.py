"""Dependency-free Project Manager CLI for candidate build, status, retry and report."""

from __future__ import annotations

import argparse
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from packages.domain.candidate_package import (
    CandidatePackageError,
    build_candidate_package,
    verify_candidate_package,
)
from packages.domain.candidates import CandidateValidationError, load_candidate
from packages.domain.dual_run import BranchWorkspace
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.publisher.project_manager import (
    ProjectManagerReportError,
    project_manager_report_bytes,
)
from packages.storage.safe_files import (
    SafeFilesystemError,
    atomic_write_new,
    open_directory_nofollow,
    read_regular_file,
    relative_parts,
)


def _common_build_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--staging-db", required=True, type=Path)
    parser.add_argument("--package-store", required=True, type=Path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar-v2-candidate")
    commands = parser.add_subparsers(dest="command", required=True)
    daily = commands.add_parser("daily")
    _common_build_arguments(daily)
    daily.add_argument("--v2-workspace", required=True, type=Path)
    correction = commands.add_parser("correction")
    _common_build_arguments(correction)
    gazette = commands.add_parser("gazette")
    _common_build_arguments(gazette)
    gazette.add_argument("--asset-root", required=True, type=Path)
    for command in ("status", "retry"):
        status = commands.add_parser(command)
        status.add_argument("--package", required=True, type=Path)
    report = commands.add_parser("report")
    report.add_argument("--publisher-result", required=True, type=Path)
    report.add_argument("--output", type=Path)
    return parser


def _load_assets(root: Path, candidate: JsonObject) -> dict[str, bytes]:
    root_descriptor = open_directory_nofollow(root)
    try:
        metadata = os.fstat(root_descriptor)
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise CandidatePackageError("asset root permissions are broader than private")
    finally:
        os.close(root_descriptor)
    assets: dict[str, bytes] = {}
    for descriptor in cast(list[dict[str, object]], candidate["inputAssets"]):
        relative = cast(str, descriptor["relativePath"])
        parts = relative_parts(relative)
        content = read_regular_file(root.joinpath(*parts))
        if len(content) > 52_428_800:
            raise CandidatePackageError(f"gazette asset exceeds 50 MiB: {relative}")
        assets[relative] = content
    return assets


def _load_json_object(path: Path) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ProjectManagerReportError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value: object = json.loads(path.read_bytes(), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectManagerReportError(f"invalid publisher-result JSON: {error}") from error
    if not isinstance(value, dict):
        raise ProjectManagerReportError("publisher result must be a JSON object")
    return cast(dict[str, object], value)


def _build(args: argparse.Namespace) -> JsonObject:
    candidate = load_candidate(cast(Path, args.candidate))
    operation = cast(str, candidate["operation"])
    if operation != args.command:
        raise CandidatePackageError(
            f"candidate operation {operation} does not match {args.command} playbook"
        )
    workspace = None
    assets: Mapping[str, bytes] | None = None
    if operation == "daily":
        snapshot = cast(dict[str, object], candidate["snapshot"])
        workspace = BranchWorkspace(
            root=cast(Path, args.v2_workspace),
            branch="v2",
            snapshot_id=cast(str, snapshot["snapshotId"]),
        )
    elif operation == "gazette":
        assets = _load_assets(cast(Path, args.asset_root), candidate)
    result = build_candidate_package(
        source_database=cast(Path, args.source_db),
        staging_database=cast(Path, args.staging_db),
        package_store=cast(Path, args.package_store),
        candidate=candidate,
        v2_workspace=workspace,
        assets=assets,
    )
    return {
        "candidateId": cast(str, candidate["candidateId"]),
        "llmStatus": cast(str, cast(dict[str, object], candidate["llmOutcome"])["status"]),
        "operation": operation,
        "packageSha256": result.package.package_sha256,
        "replay": {
            "afterStateHash": result.replay.after_state_hash,
            "applied": result.replay.applied,
            "idempotentSkips": result.replay.idempotent_skips,
        },
        "status": "candidate_ready",
    }


def _status(path: Path, *, retry: bool) -> JsonObject:
    package = verify_candidate_package(path)
    return {
        "candidateId": cast(str, package.candidate["candidateId"]),
        "disposition": "ready_for_publisher_retry" if retry else "immutable_candidate_verified",
        "llmStatus": cast(str, cast(dict[str, object], package.candidate["llmOutcome"])["status"]),
        "operation": cast(str, package.candidate["operation"]),
        "packageSha256": package.package_sha256,
        "status": "ready",
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one explicit Project Manager playbook and emit canonical machine JSON."""
    args = _parser().parse_args(argv)
    try:
        if args.command in {"daily", "correction", "gazette"}:
            output = canonical_json_line(_build(args))
        elif args.command in {"status", "retry"}:
            output = canonical_json_line(
                _status(cast(Path, args.package), retry=args.command == "retry")
            )
        else:
            output = project_manager_report_bytes(
                _load_json_object(cast(Path, args.publisher_result))
            )
            if args.output is not None:
                atomic_write_new(cast(Path, args.output), output, mode=0o600)
        os.write(1, output)
        return 0
    except (
        CandidatePackageError,
        CandidateValidationError,
        ProjectManagerReportError,
        SafeFilesystemError,
        OSError,
    ) as error:
        os.write(
            2,
            canonical_json_line(
                {
                    "error": type(error).__name__,
                    "message": str(error),
                    "status": "rejected",
                }
            ),
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
