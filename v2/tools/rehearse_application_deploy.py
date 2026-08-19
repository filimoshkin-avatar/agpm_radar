"""Run the manual-approved Stage 9 source/production deployment on one private simulation root."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

V2_ROOT: Final = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from packages.deployment.local_release import (  # noqa: E402
    ApplicationTarget,
    ManualApplicationApproval,
    deploy_application_release,
    deployment_report_document,
)
from packages.storage.safe_files import read_regular_file  # noqa: E402


def _target(simulation_root: Path, name: str) -> ApplicationTarget:
    endpoint = simulation_root / name
    return ApplicationTarget(
        name=name,
        api_root=endpoint / "api",
        web_root=endpoint / "web",
        content_root=endpoint / "content",
    )


def main(argv: list[str] | None = None) -> int:
    """Activate only below the explicit simulation root and print path-free JSON evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulation-root", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--approved-release-id", required=True)
    parser.add_argument("--approved-git-commit", required=True)
    parser.add_argument("--approved-package-sha256", required=True)
    parser.add_argument("--activated-at", required=True)
    parser.add_argument("--prove-rollback", action="store_true")
    arguments = parser.parse_args(argv)
    simulation_root = arguments.simulation_root.resolve(strict=True)
    if simulation_root == Path("/") or len(simulation_root.parts) < 3:
        raise RuntimeError("simulation root is too broad")
    package = read_regular_file(arguments.package)
    report = deploy_application_release(
        package,
        approval=ManualApplicationApproval(
            approval_id=arguments.approval_id,
            application_release_id=arguments.approved_release_id,
            git_commit=arguments.approved_git_commit,
            package_sha256=arguments.approved_package_sha256,
        ),
        source=_target(simulation_root, "source"),
        production=_target(simulation_root, "production"),
        work_root=simulation_root / "mutation",
        activated_at=arguments.activated_at,
        prove_rollback=arguments.prove_rollback,
    )
    print(
        json.dumps(
            deployment_report_document(report),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
