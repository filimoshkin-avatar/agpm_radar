"""Stage 9 immutable application release, migration and rollback regressions."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

import jsonschema  # type: ignore[import-untyped]
import pytest
from apps.api.database import ActiveDatabaseManager
from apps.migration_runner.__main__ import main as migration_main
from packages.deployment import artifacts as artifact_module
from packages.deployment.artifacts import (
    API_ARTIFACT,
    APPLICATION_ARTIFACTS,
    MIGRATION_ARTIFACT,
    PUBLIC_PRODUCTION_ARTIFACT,
    WEB_ARTIFACT,
    ApplicationArtifactError,
    BuiltApplicationRelease,
    build_application_release,
    build_role_artifact,
    collect_artifact_files,
    parse_application_release,
    parse_role_artifact,
)
from packages.deployment.local_release import (
    ActiveTargetState,
    ApplicationApprovalError,
    ApplicationDeployError,
    ApplicationTarget,
    ManualApplicationApproval,
    SimulatedApplicationFailureError,
    deploy_application_release,
    deployment_report_document,
    install_initial_application,
)
from packages.deployment.manifest import canonical_json_bytes
from packages.deployment.migration import ApplicationMigrationError, migrate_staging_connection
from packages.storage.content_pointer import read_content_pointer
from packages.storage.hashing import database_digest, logical_state_hash
from packages.storage.migrations import apply_migrations, discover_migrations
from packages.storage.mutation_lock import acquire_mutation_lock, release_mutation_lock
from packages.storage.safe_files import (
    SafeFilesystemError,
    atomic_write_new,
    publish_tree_directory,
    read_regular_file,
)
from packages.storage.sqlite_profile import REQUIRED_SQLITE_PROFILE
from tools.build_application_release import GitProvenanceError, verify_clean_git_provenance

ROOT: Final = Path(__file__).resolve().parents[2]
V2_ROOT: Final = ROOT / "v2"
CONTRACT_PATH: Final = ROOT / "contracts/v1/compatibility-manifest.schema.json"
CREATED_AT: Final = "2026-08-19T19:10:00Z"
ACTIVATED_AT: Final = "2026-08-19T19:20:00Z"
GIT_COMMIT: Final = "1" * 40
BASE_APPLICATION_RELEASE: Final = "app_release_stage8_base"
BASE_CONTENT_RELEASE: Final = "content_release_stage9_base"


@dataclass(frozen=True, slots=True)
class Rehearsal:
    source: ApplicationTarget
    production: ApplicationTarget
    source_state: ActiveTargetState
    production_state: ActiveTargetState
    work_root: Path
    base_database: Path
    base_sha256: str


@pytest.fixture(scope="module")
def built_release() -> BuiltApplicationRelease:
    return build_application_release(
        V2_ROOT,
        application_release_id="app_release_stage9_test",
        git_commit=GIT_COMMIT,
        created_at=CREATED_AT,
    )


def _reserve_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    os.close(descriptor)


def _base_database(path: Path) -> str:
    """Create a valid pre-Stage-8 DB so Stage 9 must apply migration 0002."""
    _reserve_database(path)
    migrations = discover_migrations()
    assert tuple(item.version for item in migrations)[:2] == ("0001", "0002")
    with sqlite3.connect(path) as connection:
        apply_migrations(
            connection,
            applied_at="2026-08-19T18:00:00Z",
            migrations=(migrations[0],),
        )
        connection.execute(
            """
            INSERT INTO application_compatibility VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                BASE_APPLICATION_RELEASE,
                "1.0.0",
                "1.0.0",
                "1.0.0",
                "1.0.0",
                "1.0.0",
                "1.0.0",
                REQUIRED_SQLITE_PROFILE.version,
                "2026-08-19T18:00:00Z",
            ),
        )
        state_hash = logical_state_hash(connection)
        connection.execute(
            """
            INSERT INTO content_releases VALUES (?, 1, NULL, ?, 'daily', 1, ?, ?, ?, ?)
            """,
            (
                BASE_CONTENT_RELEASE,
                "candidate_stage9_base",
                state_hash,
                state_hash,
                "2026-08-19T18:00:00Z",
                "2026-08-19T18:00:00Z",
            ),
        )
        connection.commit()
    os.chmod(path, 0o600)
    return state_hash


def _target(root: Path, name: str) -> ApplicationTarget:
    endpoint = root / name
    return ApplicationTarget(
        name=name,
        api_root=endpoint / "api",
        web_root=endpoint / "web",
        content_root=endpoint / "content",
    )


def _rehearsal(tmp_path: Path) -> Rehearsal:
    root = tmp_path / "application-rehearsal"
    root.mkdir(mode=0o700)
    base_database = root / "base.sqlite"
    _base_database(base_database)
    base_sha256 = hashlib.sha256(base_database.read_bytes()).hexdigest()
    source = _target(root, "source")
    production = _target(root, "production")
    (root / "source").mkdir(mode=0o700)
    (root / "production").mkdir(mode=0o700)
    from packages.publisher.local_simulation import install_initial_release

    install_initial_release(source.content_root, base_database)
    install_initial_release(production.content_root, base_database)
    baseline_api = {
        "APPLICATION-RELEASE.json": b'{"applicationReleaseId":"app_release_stage8_base"}\n',
        "apps/api/version.txt": b"stage-8\n",
    }
    baseline_web = {
        "APPLICATION-RELEASE.json": b'{"applicationReleaseId":"app_release_stage8_base"}\n',
        "apps/web/index.html": b"<!doctype html><title>stage-8</title>\n",
    }
    source_state = install_initial_application(
        source,
        release_id=BASE_APPLICATION_RELEASE,
        api_files=baseline_api,
        web_files=baseline_web,
    )
    production_state = install_initial_application(
        production,
        release_id=BASE_APPLICATION_RELEASE,
        api_files=baseline_api,
        web_files=baseline_web,
    )
    return Rehearsal(
        source=source,
        production=production,
        source_state=source_state,
        production_state=production_state,
        work_root=root / "mutation",
        base_database=base_database,
        base_sha256=base_sha256,
    )


def _approval(release: BuiltApplicationRelease) -> ManualApplicationApproval:
    return ManualApplicationApproval(
        approval_id="ivan-stage9-test-approval",
        application_release_id=release.manifest.application_release_id,
        git_commit=release.manifest.git_commit,
        package_sha256=release.sha256,
    )


def _current(root: Path) -> str:
    return os.readlink(root / "current")


def _assert_prior_active(rehearsal: Rehearsal) -> None:
    assert _current(rehearsal.source.api_root) == rehearsal.source_state.api_target
    assert _current(rehearsal.source.web_root) == rehearsal.source_state.web_target
    assert _current(rehearsal.production.api_root) == rehearsal.production_state.api_target
    assert _current(rehearsal.production.web_root) == rehearsal.production_state.web_target
    assert (
        read_regular_file(rehearsal.source.content_root / "active.json", expected_mode=0o600)
        == rehearsal.source_state.content_pointer
    )
    assert (
        read_regular_file(rehearsal.production.content_root / "active.json", expected_mode=0o600)
        == rehearsal.production_state.content_pointer
    )


def _build_bad_archive(member: tarfile.TarInfo, payload: bytes = b"x") -> bytes:
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        member.size = len(payload) if member.isreg() else 0
        archive.addfile(member, io.BytesIO(payload) if member.isreg() else None)
    gzip_buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=gzip_buffer, mode="wb", filename="", mtime=0) as compressor:
        compressor.write(tar_buffer.getvalue())
    return gzip_buffer.getvalue()


def test_role_and_outer_artifacts_are_reproducible_and_publicly_minimal(
    built_release: BuiltApplicationRelease,
) -> None:
    repeated = build_application_release(
        V2_ROOT,
        application_release_id=built_release.manifest.application_release_id,
        git_commit=GIT_COMMIT,
        created_at=CREATED_AT,
    )
    assert repeated.content == built_release.content
    assert repeated.sha256 == built_release.sha256
    parsed = parse_application_release(built_release.content)
    schema = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        parsed.manifest.document
    )
    assert parsed.provenance.git_commit == GIT_COMMIT
    assert len(parsed.provenance.source_tree_sha256) == 64
    assert tuple(item.kind for item in parsed.manifest.artifacts) == (
        "api",
        "migration-bundle",
        "web",
    )

    api_files = parse_role_artifact(parsed.artifacts[API_ARTIFACT.name], API_ARTIFACT)
    web_files = parse_role_artifact(parsed.artifacts[WEB_ARTIFACT.name], WEB_ARTIFACT)
    migration_files = parse_role_artifact(
        parsed.artifacts[MIGRATION_ARTIFACT.name], MIGRATION_ARTIFACT
    )
    assert "apps/api/__main__.py" in api_files
    assert "apps/web/app.mjs" in web_files
    assert "packages/storage/migrations/0002_public_api_views.sql" in migration_files
    assert "apps/migration_runner/__main__.py" in migration_files
    assert "deploy/templates/radar-v2-api.service" in migration_files
    service = migration_files["deploy/templates/radar-v2-api.service"].decode("utf-8")
    for required in (
        "User=radar-v2-api",
        "Group=radar-v2-api",
        "--host 127.0.0.1 --port 8765",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "ProtectKernelTunables=true",
        "ProtectControlGroups=true",
        "RestrictSUIDSGID=true",
        "RestrictNamespaces=true",
        "LockPersonality=true",
        "MemoryDenyWriteExecute=true",
        "CapabilityBoundingSet=",
        "IPAddressDeny=any",
        "IPAddressAllow=localhost",
        "ReadOnlyPaths=@API_CURRENT@ @CONTENT_ROOT@ @GAZETTE_ROOT@",
        "TasksMax=128",
        "MemoryMax=512M",
    ):
        assert required in service
    assert "User=radar-v2\n" not in service
    caddy = migration_files["deploy/templates/Caddyfile.radar-v2"].decode("utf-8")
    assert "reverse_proxy 127.0.0.1:8765" in caddy
    assert "reverse_proxy 0.0.0.0" not in caddy
    forbidden = ("candidate_builder", "legacy_bridge", "packages/publisher", "packages/delta")
    assert not any(any(fragment in path for fragment in forbidden) for path in api_files)
    assert not any(any(fragment in path for fragment in forbidden) for path in web_files)

    public = build_role_artifact(V2_ROOT, PUBLIC_PRODUCTION_ARTIFACT)
    public_files = parse_role_artifact(public.content, PUBLIC_PRODUCTION_ARTIFACT)
    assert not any(any(fragment in path for fragment in forbidden) for path in public_files)
    assert set(public.files) == set(API_ARTIFACT.paths + WEB_ARTIFACT.paths)


def test_outer_and_inner_archive_tampering_fails_closed(
    built_release: BuiltApplicationRelease,
) -> None:
    tampered = bytearray(built_release.content)
    tampered[len(tampered) // 2] ^= 1
    with pytest.raises((ApplicationArtifactError, EOFError, OSError)):
        parse_application_release(bytes(tampered))

    traversal = tarfile.TarInfo("radar-v2-api/../escape")
    traversal.mode = 0o644
    traversal.mtime = traversal.uid = traversal.gid = 0
    with pytest.raises(ApplicationArtifactError):
        parse_role_artifact(_build_bad_archive(traversal), API_ARTIFACT)

    symlink = tarfile.TarInfo("radar-v2-api/apps")
    symlink.type = tarfile.SYMTYPE
    symlink.linkname = "../../escape"
    symlink.mode = 0o644
    symlink.mtime = symlink.uid = symlink.gid = 0
    with pytest.raises(ApplicationArtifactError):
        parse_role_artifact(_build_bad_archive(symlink), API_ARTIFACT)

    hardlink = tarfile.TarInfo("radar-v2-api/apps")
    hardlink.type = tarfile.LNKTYPE
    hardlink.linkname = "radar-v2-api/other"
    hardlink.mode = 0o644
    hardlink.mtime = hardlink.uid = hardlink.gid = 0
    with pytest.raises(ApplicationArtifactError):
        parse_role_artifact(_build_bad_archive(hardlink), API_ARTIFACT)

    wrong_mode = tarfile.TarInfo("radar-v2-api/apps/__init__.py")
    wrong_mode.mode = 0o600
    wrong_mode.mtime = wrong_mode.uid = wrong_mode.gid = 0
    with pytest.raises(ApplicationArtifactError):
        parse_role_artifact(_build_bad_archive(wrong_mode), API_ARTIFACT)

    with pytest.raises(ApplicationArtifactError, match="not canonical"):
        parse_role_artifact(built_release.artifacts[0].content + b"trailing", API_ARTIFACT)


def test_provenance_digest_is_bound_to_inner_role_bytes(
    built_release: BuiltApplicationRelease,
) -> None:
    files = artifact_module._parse_archive(
        built_release.content,
        artifact_module.APPLICATION_PACKAGE_PREFIX,
    )
    provenance = json.loads(files[artifact_module.PROVENANCE_NAME])
    provenance["sourceTreeSha256"] = "0" * 64
    files[artifact_module.PROVENANCE_NAME] = canonical_json_bytes(provenance)
    checksum_inputs = {
        name: payload for name, payload in files.items() if name != artifact_module.CHECKSUMS_NAME
    }
    files[artifact_module.CHECKSUMS_NAME] = "".join(
        f"{artifact_module.sha256_bytes(checksum_inputs[name])}  {name}\n"
        for name in sorted(checksum_inputs)
    ).encode("ascii")
    forged = artifact_module._render_archive(
        artifact_module.APPLICATION_PACKAGE_PREFIX,
        files,
    )
    with pytest.raises(ApplicationArtifactError, match="sourceTreeSha256"):
        parse_application_release(forged)


def test_clean_git_provenance_requires_tracked_head_and_exact_tag(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    source = repository / "v2"
    source.mkdir(parents=True)
    selected: dict[str, bytes] = {}
    for spec in APPLICATION_ARTIFACTS:
        selected.update(collect_artifact_files(V2_ROOT, spec))
    for relative, content in selected.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def git(*arguments: str) -> str:
        completed = subprocess.run(  # noqa: S603
            ["/usr/bin/git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "stage9@example.invalid")
    git("config", "user.name", "Stage 9")
    git("add", "v2")
    git("commit", "-qm", "stage9 fixture")
    commit = git("rev-parse", "HEAD")
    git("tag", "stage9-test")
    verify_clean_git_provenance(
        repository,
        source,
        git_commit=commit,
        git_tag="stage9-test",
    )
    with pytest.raises(GitProvenanceError):
        verify_clean_git_provenance(
            repository,
            source,
            git_commit="0" * 40,
            git_tag=None,
        )
    (source / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(GitProvenanceError):
        verify_clean_git_provenance(
            repository,
            source,
            git_commit=commit,
            git_tag="stage9-test",
        )


def test_manual_approved_deploy_migrates_both_and_proves_rollback(
    tmp_path: Path,
    built_release: BuiltApplicationRelease,
) -> None:
    rehearsal = _rehearsal(tmp_path)
    report = deploy_application_release(
        built_release.content,
        approval=_approval(built_release),
        source=rehearsal.source,
        production=rehearsal.production,
        work_root=rehearsal.work_root,
        activated_at=ACTIVATED_AT,
        prove_rollback=True,
    )
    assert report.rollback_proven is True
    assert report.source_schema_sha256 == report.production_schema_sha256
    assert report.source_content_release_id == BASE_CONTENT_RELEASE
    assert report.production_content_release_id == BASE_CONTENT_RELEASE
    assert report.activation_order[:8] == (
        "source-content",
        "source-api",
        "source-web",
        "source-smoke",
        "production-content",
        "production-api",
        "production-web",
        "production-smoke",
    )
    assert report.rollback_order == (
        "production-web",
        "production-api",
        "production-content",
        "production-smoke",
        "source-web",
        "source-api",
        "source-content",
        "source-smoke",
    )
    assert report.final_source.api_target != rehearsal.source_state.api_target
    assert report.final_production.web_target != rehearsal.production_state.web_target
    assert deployment_report_document(report)["approvalId"] == "ivan-stage9-test-approval"

    source_pointer = read_content_pointer(rehearsal.source.content_root)
    production_pointer = read_content_pointer(rehearsal.production.content_root)
    with (
        sqlite3.connect(source_pointer.database_path) as source_db,
        sqlite3.connect(production_pointer.database_path) as production_db,
    ):
        assert tuple(
            source_db.execute("SELECT version FROM schema_migrations ORDER BY version")
        ) == (
            ("0001",),
            ("0002",),
        )
        assert source_db.execute(
            "SELECT application_release_id FROM application_compatibility ORDER BY activated_at DESC LIMIT 1"
        ).fetchone() == (built_release.manifest.application_release_id,)
        assert database_digest(source_db) == database_digest(production_db)

    source_manager = ActiveDatabaseManager(rehearsal.source.content_root)
    production_manager = ActiveDatabaseManager(rehearsal.production.content_root)
    try:
        assert source_manager.identity().release_id == BASE_CONTENT_RELEASE
        assert production_manager.identity().release_id == BASE_CONTENT_RELEASE
    finally:
        source_manager.close()
        production_manager.close()

    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-B",
            "-c",
            "from apps.api import status_payload; print(status_payload()['status'])",
        ],
        cwd=rehearsal.source.api_root / "current",
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "ready"
    assert not (rehearsal.source.api_root / "current/packages/publisher").exists()
    assert rehearsal.base_sha256 == hashlib.sha256(rehearsal.base_database.read_bytes()).hexdigest()
    with sqlite3.connect(rehearsal.base_database) as base:
        assert tuple(base.execute("SELECT version FROM schema_migrations")) == (("0001",),)
    assert rehearsal.source.api_root.joinpath(
        *rehearsal.source_state.api_target.split("/")
    ).is_dir()
    assert rehearsal.production.web_root.joinpath(
        *rehearsal.production_state.web_target.split("/")
    ).is_dir()


@pytest.mark.parametrize(
    "fail_after",
    (
        "source-content",
        "source-api",
        "source-web",
        "source-smoke",
        "production-content",
        "production-api",
        "production-web",
        "production-smoke",
    ),
)
def test_every_activation_failure_restores_all_prior_references(
    tmp_path: Path,
    built_release: BuiltApplicationRelease,
    fail_after: str,
) -> None:
    rehearsal = _rehearsal(tmp_path)
    with pytest.raises(SimulatedApplicationFailureError):
        deploy_application_release(
            built_release.content,
            approval=_approval(built_release),
            source=rehearsal.source,
            production=rehearsal.production,
            work_root=rehearsal.work_root,
            activated_at=ACTIVATED_AT,
            fail_after=fail_after,
        )
    _assert_prior_active(rehearsal)
    assert len(tuple((rehearsal.source.api_root / "releases").iterdir())) == 2
    assert len(tuple((rehearsal.production.content_root / "releases").iterdir())) == 2


def test_approval_and_shared_lock_fail_before_activation(
    tmp_path: Path,
    built_release: BuiltApplicationRelease,
) -> None:
    rehearsal = _rehearsal(tmp_path)
    wrong = ManualApplicationApproval(
        approval_id="approval-wrong",
        application_release_id=built_release.manifest.application_release_id,
        git_commit=built_release.manifest.git_commit,
        package_sha256="0" * 64,
    )
    with pytest.raises(ApplicationApprovalError):
        deploy_application_release(
            built_release.content,
            approval=wrong,
            source=rehearsal.source,
            production=rehearsal.production,
            work_root=rehearsal.work_root,
            activated_at=ACTIVATED_AT,
        )
    _assert_prior_active(rehearsal)

    descriptor = acquire_mutation_lock(rehearsal.work_root)
    try:
        with pytest.raises(ApplicationDeployError, match="mutation lock is busy"):
            deploy_application_release(
                built_release.content,
                approval=_approval(built_release),
                source=rehearsal.source,
                production=rehearsal.production,
                work_root=rehearsal.work_root,
                activated_at=ACTIVATED_AT,
            )
    finally:
        release_mutation_lock(descriptor)
    _assert_prior_active(rehearsal)

    overlapping = ApplicationTarget(
        name="production",
        api_root=rehearsal.source.api_root,
        web_root=rehearsal.production.web_root,
        content_root=rehearsal.production.content_root,
    )
    with pytest.raises(ApplicationDeployError, match="overlap"):
        deploy_application_release(
            built_release.content,
            approval=_approval(built_release),
            source=rehearsal.source,
            production=overlapping,
            work_root=rehearsal.work_root,
            activated_at=ACTIVATED_AT,
        )


def test_staging_parity_rejects_metadata_drift_without_activation(
    tmp_path: Path,
    built_release: BuiltApplicationRelease,
) -> None:
    rehearsal = _rehearsal(tmp_path)
    production_pointer = read_content_pointer(rehearsal.production.content_root)
    with sqlite3.connect(production_pointer.database_path) as connection:
        connection.execute(
            "INSERT INTO application_compatibility VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "app_release_metadata_drift",
                "1.0.0",
                "1.0.0",
                "1.0.0",
                "1.0.0",
                "1.0.0",
                "1.0.0",
                REQUIRED_SQLITE_PROFILE.version,
                "2026-08-19T17:00:00Z",
            ),
        )
        connection.commit()
    with pytest.raises(ApplicationDeployError, match="migration reports differ"):
        deploy_application_release(
            built_release.content,
            approval=_approval(built_release),
            source=rehearsal.source,
            production=rehearsal.production,
            work_root=rehearsal.work_root,
            activated_at=ACTIVATED_AT,
        )
    _assert_prior_active(rehearsal)


def test_release_id_cannot_be_rebound_to_another_commit(
    tmp_path: Path,
    built_release: BuiltApplicationRelease,
) -> None:
    rehearsal = _rehearsal(tmp_path)
    deploy_application_release(
        built_release.content,
        approval=_approval(built_release),
        source=rehearsal.source,
        production=rehearsal.production,
        work_root=rehearsal.work_root,
        activated_at=ACTIVATED_AT,
        prove_rollback=False,
    )
    rebound = build_application_release(
        V2_ROOT,
        application_release_id=built_release.manifest.application_release_id,
        git_commit="2" * 40,
        created_at="2026-08-19T19:30:00Z",
    )
    with pytest.raises(ApplicationDeployError, match="already has other bytes"):
        deploy_application_release(
            rebound.content,
            approval=_approval(rebound),
            source=rehearsal.source,
            production=rehearsal.production,
            work_root=rehearsal.work_root,
            activated_at="2026-08-19T19:40:00Z",
        )


def test_activation_time_and_runtime_manifest_drift_fail_before_content_change(
    tmp_path: Path,
    built_release: BuiltApplicationRelease,
) -> None:
    rehearsal = _rehearsal(tmp_path)
    with pytest.raises(ApplicationDeployError, match="timestamp"):
        deploy_application_release(
            built_release.content,
            approval=_approval(built_release),
            source=rehearsal.source,
            production=rehearsal.production,
            work_root=rehearsal.work_root,
            activated_at="2026-08-19 19:20:00",
        )
    _assert_prior_active(rehearsal)

    drifted = replace(
        built_release.manifest,
        sqlite_compile_options=(*built_release.manifest.sqlite_compile_options, "FAKE_OPTION"),
    )
    database = tmp_path / "runtime-drift.sqlite"
    _base_database(database)
    with sqlite3.connect(database) as connection:
        with pytest.raises(ApplicationMigrationError, match="runtime differs"):
            migrate_staging_connection(
                connection,
                manifest=drifted,
                activated_at=ACTIVATED_AT,
                migrations=discover_migrations(),
            )
        assert tuple(connection.execute("SELECT version FROM schema_migrations")) == (("0001",),)


def test_migration_bundle_runner_is_self_contained_and_locked(
    tmp_path: Path,
    built_release: BuiltApplicationRelease,
) -> None:
    base = tmp_path / "base.sqlite"
    state_hash = _base_database(base)
    staging = tmp_path / "staging.sqlite"
    shutil.copyfile(base, staging)
    os.chmod(staging, 0o600)
    migration_artifact = next(
        artifact for artifact in built_release.artifacts if artifact.spec == MIGRATION_ARTIFACT
    )
    bundle_files = parse_role_artifact(migration_artifact.content, MIGRATION_ARTIFACT)
    bundle = publish_tree_directory(tmp_path, "migration-bundle", bundle_files)
    manifest_path = tmp_path / "compatibility-manifest.json"
    atomic_write_new(manifest_path, built_release.manifest_bytes, mode=0o600)
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-B",
            "-m",
            "apps.migration_runner",
            "--staging-database",
            str(staging),
            "--compatibility-manifest",
            str(manifest_path),
            "--migrations",
            str(bundle / "packages/storage/migrations"),
            "--lock-root",
            str(tmp_path / "runner-lock"),
            "--activated-at",
            ACTIVATED_AT,
            "--expected-state-hash",
            state_hash,
        ],
        cwd=bundle,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert report["appliedMigrations"] == ["0002"]
    assert report["stateHash"] == state_hash
    with sqlite3.connect(staging) as connection:
        assert tuple(
            connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ) == (
            ("0001",),
            ("0002",),
        )

    common_arguments = [
        "--compatibility-manifest",
        str(manifest_path),
        "--migrations",
        str(bundle / "packages/storage/migrations"),
        "--lock-root",
        str(tmp_path / "runner-negative-lock"),
        "--activated-at",
        ACTIVATED_AT,
        "--expected-state-hash",
        state_hash,
    ]
    symlink = tmp_path / "symlink-staging.sqlite"
    symlink.symlink_to(base)
    with pytest.raises(SafeFilesystemError):
        migration_main(["--staging-database", str(symlink), *common_arguments])
    hardlink = tmp_path / "hardlink-staging.sqlite"
    os.link(base, hardlink)
    with pytest.raises(SafeFilesystemError):
        migration_main(["--staging-database", str(hardlink), *common_arguments])
