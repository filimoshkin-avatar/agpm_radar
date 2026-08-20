"""Closed CLI for one manually approved Radar V2 remote publication."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from packages.domain.snapshot import canonical_json_line
from packages.publisher.project_manager import project_manager_report_bytes
from packages.publisher.remote_orchestration import (
    PublishInputs,
    RemoteOrchestrationError,
    publish_candidate,
    ssh_transport,
)
from packages.storage.safe_files import SafeFilesystemError, atomic_write_new


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar-v2-publisher")
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--candidate-staging", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--application-release-id", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--finished-at", required=True)
    parser.add_argument("--duration-ms", required=True, type=int)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-identity", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Publish one exact candidate and persist both machine and owner-facing outputs."""
    args = _parser().parse_args(argv)
    try:
        result = publish_candidate(
            PublishInputs(
                package=args.package,
                candidate_staging=args.candidate_staging,
                source_root=args.source_root,
                work_root=args.work_root,
                application_release_id=args.application_release_id,
                created_at=args.created_at,
                finished_at=args.finished_at,
                duration_ms=args.duration_ms,
            ),
            ssh_transport(host=args.ssh_host, identity=args.ssh_identity),
        )
        result_bytes = canonical_json_line(result)
        report_bytes = project_manager_report_bytes(result)
        atomic_write_new(args.result, result_bytes, mode=0o600)
        atomic_write_new(args.report, report_bytes, mode=0o600)
        os.write(1, report_bytes)
        return 0
    except (OSError, RemoteOrchestrationError, SafeFilesystemError, ValueError) as error:
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
