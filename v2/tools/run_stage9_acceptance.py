"""Create retained local evidence for a clean-commit Stage 9 application release."""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from pathlib import Path
from typing import Final

V2_ROOT: Final = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from apps.api.database import ActiveDatabaseManager  # noqa: E402
from packages.deployment.local_release import (  # noqa: E402
    ApplicationTarget,
    ManualApplicationApproval,
    deploy_application_release,
    deployment_report_document,
    install_initial_application,
)
from packages.deployment.manifest import canonical_json_bytes  # noqa: E402
from packages.publisher.local_simulation import install_initial_release  # noqa: E402
from packages.storage.content_pointer import read_content_pointer  # noqa: E402
from packages.storage.hashing import database_digest, logical_state_hash  # noqa: E402
from packages.storage.migrations import apply_migrations, discover_migrations  # noqa: E402
from packages.storage.safe_files import (  # noqa: E402
    atomic_write_new,
    create_private_directory,
    ensure_private_directory,
    read_regular_file,
)
from packages.storage.sqlite_profile import REQUIRED_SQLITE_PROFILE  # noqa: E402

BASE_APPLICATION_RELEASE: Final = "app_release_stage8_acceptance"
BASE_CONTENT_RELEASE: Final = "content_release_stage9_acceptance"
BASE_ACTIVATED_AT: Final = "2026-08-19T18:00:00Z"


def _reserve_database(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.close(descriptor)


def _build_base_database(path: Path) -> str:
    _reserve_database(path)
    migrations = discover_migrations()
    if tuple(migration.version for migration in migrations)[:2] != ("0001", "0002"):
        raise RuntimeError("Stage 9 acceptance requires migrations 0001 and 0002")
    with sqlite3.connect(path) as connection:
        apply_migrations(
            connection,
            applied_at=BASE_ACTIVATED_AT,
            migrations=(migrations[0],),
        )
        connection.execute(
            "INSERT INTO application_compatibility VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                BASE_APPLICATION_RELEASE,
                "1.0.0",
                "1.0.0",
                "1.0.0",
                "1.0.0",
                "1.0.0",
                "1.0.0",
                REQUIRED_SQLITE_PROFILE.version,
                BASE_ACTIVATED_AT,
            ),
        )
        state_hash = logical_state_hash(connection)
        connection.execute(
            "INSERT INTO content_releases VALUES (?, 1, NULL, ?, 'daily', 1, ?, ?, ?, ?)",
            (
                BASE_CONTENT_RELEASE,
                "candidate_stage9_acceptance",
                state_hash,
                state_hash,
                BASE_ACTIVATED_AT,
                BASE_ACTIVATED_AT,
            ),
        )
        connection.commit()
    os.chmod(path, 0o600)
    return state_hash


def _target(root: Path, name: str) -> ApplicationTarget:
    endpoint = root / name
    create_private_directory(endpoint)
    return ApplicationTarget(
        name=name,
        api_root=endpoint / "api",
        web_root=endpoint / "web",
        content_root=endpoint / "content",
    )


def _install_baseline(target: ApplicationTarget, database: Path) -> dict[str, object]:
    install_initial_release(target.content_root, database)
    state = install_initial_application(
        target,
        release_id=BASE_APPLICATION_RELEASE,
        api_files={
            "APPLICATION-RELEASE.json": canonical_json_bytes(
                {"applicationReleaseId": BASE_APPLICATION_RELEASE}
            ),
            "apps/api/version.txt": b"stage-8-baseline\n",
        },
        web_files={
            "APPLICATION-RELEASE.json": canonical_json_bytes(
                {"applicationReleaseId": BASE_APPLICATION_RELEASE}
            ),
            "apps/web/index.html": b"<!doctype html><title>Stage 8 baseline</title>\n",
        },
    )
    return {
        "apiTarget": state.api_target,
        "contentPointerSha256": hashlib.sha256(state.content_pointer).hexdigest(),
        "webTarget": state.web_target,
    }


def _database_evidence(target: ApplicationTarget) -> dict[str, object]:
    pointer = read_content_pointer(target.content_root)
    with sqlite3.connect(pointer.database_path) as connection:
        digest = database_digest(connection)
        migrations = [
            str(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        compatibility = connection.execute(
            """
            SELECT application_release_id
            FROM application_compatibility
            ORDER BY activated_at DESC, application_release_id DESC
            LIMIT 1
            """
        ).fetchone()
    return {
        "applicationReleaseId": str(compatibility[0]),
        "contentReleaseId": pointer.release_id,
        "logicalStateHash": digest.state_hash,
        "migrationVersions": migrations,
        "replicatedTableCounts": digest.table_counts,
        "replicatedTableHashes": digest.table_hashes,
    }


def main(argv: list[str] | None = None) -> int:
    """Run a two-target activation/rollback/re-activation and retain every artifact."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--approved-release-id", required=True)
    parser.add_argument("--approved-git-commit", required=True)
    parser.add_argument("--approved-package-sha256", required=True)
    parser.add_argument("--activated-at", required=True)
    arguments = parser.parse_args(argv)

    create_private_directory(arguments.evidence_root)
    baseline_root = arguments.evidence_root / "baseline"
    ensure_private_directory(baseline_root)
    baseline_database = baseline_root / "pre-stage9.sqlite"
    baseline_state_hash = _build_base_database(baseline_database)
    baseline_before = hashlib.sha256(baseline_database.read_bytes()).hexdigest()
    source = _target(arguments.evidence_root, "source")
    production = _target(arguments.evidence_root, "production")
    source_previous = _install_baseline(source, baseline_database)
    production_previous = _install_baseline(production, baseline_database)

    package = read_regular_file(arguments.package)
    report = deploy_application_release(
        package,
        approval=ManualApplicationApproval(
            approval_id=arguments.approval_id,
            application_release_id=arguments.approved_release_id,
            git_commit=arguments.approved_git_commit,
            package_sha256=arguments.approved_package_sha256,
        ),
        source=source,
        production=production,
        work_root=arguments.evidence_root / "mutation",
        activated_at=arguments.activated_at,
        prove_rollback=True,
    )
    source_manager = ActiveDatabaseManager(source.content_root)
    production_manager = ActiveDatabaseManager(production.content_root)
    try:
        source_identity = source_manager.identity()
        production_identity = production_manager.identity()
    finally:
        source_manager.close()
        production_manager.close()
    source_evidence = _database_evidence(source)
    production_evidence = _database_evidence(production)
    if source_evidence != production_evidence:
        raise RuntimeError("accepted source/production database evidence differs")
    if baseline_before != hashlib.sha256(baseline_database.read_bytes()).hexdigest():
        raise RuntimeError("acceptance baseline database changed")
    evidence = {
        "acceptanceFormat": "radar-v2-stage9-local/v1",
        "baselineDatabaseSha256": baseline_before,
        "baselineLogicalStateHash": baseline_state_hash,
        "deployment": deployment_report_document(report),
        "previousProduction": production_previous,
        "previousSource": source_previous,
        "productionDatabase": production_evidence,
        "productionIdentity": {
            "releaseId": production_identity.release_id,
            "schemaVersion": production_identity.schema_version,
            "stateHash": production_identity.state_hash,
        },
        "sourceDatabase": source_evidence,
        "sourceIdentity": {
            "releaseId": source_identity.release_id,
            "schemaVersion": source_identity.schema_version,
            "stateHash": source_identity.state_hash,
        },
        "testOnlyApproval": True,
    }
    evidence_path = arguments.evidence_root / "acceptance.json"
    atomic_write_new(evidence_path, canonical_json_bytes(evidence), mode=0o600)
    print("Radar V2 Stage 9 local acceptance: PASS")
    print(f"Evidence: {evidence_path}")
    print(f"Application release: {report.application_release_id}")
    print(f"Package SHA-256: {report.package_sha256}")
    print(f"Schema SHA-256: {report.source_schema_sha256}")
    print(f"Rollback proven: {report.rollback_proven}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
