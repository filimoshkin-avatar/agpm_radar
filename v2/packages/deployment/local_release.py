"""Manual-approved disposable source/production application deployment rehearsal."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from packages.delta.engine import inspect_release_database
from packages.deployment.artifacts import (
    API_ARTIFACT,
    MIGRATION_ARTIFACT,
    WEB_ARTIFACT,
    ParsedApplicationRelease,
    parse_application_release,
    parse_role_artifact,
    sha256_bytes,
)
from packages.deployment.manifest import (
    ApplicationManifest,
    canonical_json_bytes,
    validate_utc_timestamp,
)
from packages.deployment.migration import (
    ApplicationMigrationReport,
    migrate_staging_connection,
    migrations_from_artifact_files,
)
from packages.storage.content_pointer import (
    ContentPointer,
    parse_content_pointer,
    read_content_pointer,
)
from packages.storage.hashing import database_digest, logical_state_hash
from packages.storage.mutation_lock import (
    MutationLockBusyError,
    acquire_mutation_lock,
    release_mutation_lock,
)
from packages.storage.safe_files import (
    SafeFilesystemError,
    atomic_write_new,
    ensure_private_directory,
    open_directory_nofollow,
    open_regular_file_nofollow,
    publish_tree_directory,
    read_regular_file,
    read_regular_file_at,
    read_tree_files,
    relative_parts,
)
from packages.validation.public_issue import verify_public_database_connection

_CURRENT_NAME: Final = "current"
_ACTIVE_POINTER_NAME: Final = "active.json"
_APPROVAL_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_FAILURE_STAGES: Final = frozenset(
    {
        "source-content",
        "source-api",
        "source-web",
        "source-smoke",
        "production-content",
        "production-api",
        "production-web",
        "production-smoke",
    }
)


class ApplicationDeployError(RuntimeError):
    """The approved application release could not be staged or activated safely."""


class ApplicationApprovalError(ApplicationDeployError):
    """Manual approval does not exactly bind the release package being deployed."""


class ApplicationRollbackError(ApplicationDeployError):
    """A coordinated rollback could not restore every prior active pointer."""


class SimulatedApplicationFailureError(ApplicationDeployError):
    """Test-only fault injected after one named activation step."""


@dataclass(frozen=True, slots=True)
class ManualApplicationApproval:
    """Exact human approval tuple required before any application activation."""

    approval_id: str
    application_release_id: str
    git_commit: str
    package_sha256: str


@dataclass(frozen=True, slots=True)
class ApplicationTarget:
    """One source or production endpoint represented by three private roots."""

    name: str
    api_root: Path
    web_root: Path
    content_root: Path


@dataclass(frozen=True, slots=True)
class ActiveTargetState:
    """All three independently switchable active references for one endpoint."""

    api_target: str
    web_target: str
    content_pointer: bytes


@dataclass(frozen=True, slots=True)
class PreparedTarget:
    """Immutable application trees and migrated DB ready for activation."""

    target: ApplicationTarget
    previous: ActiveTargetState
    api_target: str
    web_target: str
    content_pointer: bytes
    migration: ApplicationMigrationReport


@dataclass(frozen=True, slots=True)
class ApplicationDeployReport:
    """Structured evidence for activation, rollback proof and final state."""

    application_release_id: str
    approval_id: str
    git_commit: str
    package_sha256: str
    source_schema_sha256: str
    production_schema_sha256: str
    source_content_release_id: str
    production_content_release_id: str
    activation_order: tuple[str, ...]
    rollback_order: tuple[str, ...]
    rollback_proven: bool
    final_source: ActiveTargetState
    final_production: ActiveTargetState


def _application_token(release_id: str) -> str:
    return hashlib.sha256(release_id.encode("utf-8")).hexdigest()[:32]


def _content_relative(pointer: ContentPointer, application_release_id: str) -> str:
    base = hashlib.sha256(pointer.release_id.encode("utf-8")).hexdigest()[:24]
    application = _application_token(application_release_id)[:24]
    return f"releases/{base}.{application}.sqlite"


def _validate_approval(
    release: ParsedApplicationRelease,
    approval: ManualApplicationApproval,
) -> None:
    if not _APPROVAL_ID.fullmatch(approval.approval_id):
        raise ApplicationApprovalError("manual approval id is invalid")
    expected = (
        release.manifest.application_release_id,
        release.manifest.git_commit,
        release.sha256,
    )
    observed = (
        approval.application_release_id,
        approval.git_commit,
        approval.package_sha256,
    )
    if observed != expected:
        raise ApplicationApprovalError("manual approval does not match release id/commit/package")


def _ensure_target_roots(target: ApplicationTarget) -> None:
    if not target.name or len(target.name) > 64:
        raise ApplicationDeployError("application target name is missing or unbounded")
    for root in (target.api_root, target.web_root, target.content_root):
        ensure_private_directory(root)
        ensure_private_directory(root / "releases")


def _validate_target_topology(
    source: ApplicationTarget,
    production: ApplicationTarget,
    work_root: Path,
) -> None:
    roots = tuple(
        Path(os.path.abspath(path))
        for path in (
            source.api_root,
            source.web_root,
            source.content_root,
            production.api_root,
            production.web_root,
            production.content_root,
            work_root,
        )
    )
    if len(set(roots)) != len(roots):
        raise ApplicationDeployError("application deployment roots overlap")
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if root in other.parents or other in root.parents:
                raise ApplicationDeployError("application deployment roots must not be nested")


def _release_target(release_id: str) -> str:
    return f"releases/{_application_token(release_id)}"


def _read_current(root: Path) -> str:
    directory = open_directory_nofollow(root)
    try:
        metadata = os.stat(_CURRENT_NAME, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1:
            raise ApplicationDeployError(f"application current is not a single symlink: {root}")
        target = os.readlink(_CURRENT_NAME, dir_fd=directory)
    except OSError as error:
        raise ApplicationDeployError(f"cannot read application current symlink: {root}") from error
    finally:
        os.close(directory)
    try:
        parts = relative_parts(target)
    except SafeFilesystemError as error:
        raise ApplicationDeployError(f"application current target is unsafe: {target}") from error
    if len(parts) != 2 or parts[0] != "releases":
        raise ApplicationDeployError("application current target is outside releases")
    read_tree_files(root.joinpath(*parts))
    return target


def _replace_current(root: Path, target: str, expected_current: str) -> None:
    parts = relative_parts(target)
    if len(parts) != 2 or parts[0] != "releases":
        raise ApplicationDeployError("replacement application target is outside releases")
    read_tree_files(root.joinpath(*parts))
    if _read_current(root) != expected_current:
        raise ApplicationDeployError("application current changed before atomic activation")
    directory = open_directory_nofollow(root)
    temporary = f".current.{hashlib.sha256(target.encode()).hexdigest()[:24]}.next"
    created = False
    try:
        current_metadata = os.stat(_CURRENT_NAME, dir_fd=directory, follow_symlinks=False)
        if (
            not stat.S_ISLNK(current_metadata.st_mode)
            or current_metadata.st_nlink != 1
            or os.readlink(_CURRENT_NAME, dir_fd=directory) != expected_current
        ):
            raise ApplicationDeployError("application current changed before pinned activation")
        try:
            os.symlink(target, temporary, dir_fd=directory)
            created = True
        except FileExistsError:
            existing = os.readlink(temporary, dir_fd=directory)
            if existing != target:
                raise ApplicationDeployError(
                    "stale current staging symlink has another target"
                ) from None
        os.replace(temporary, _CURRENT_NAME, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
        os.fsync(directory)
    except OSError as error:
        raise ApplicationDeployError(f"atomic application activation failed: {error}") from error
    finally:
        if created:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
        os.close(directory)


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise ApplicationDeployError("short active-pointer write")
        remaining = remaining[written:]


def _replace_content_pointer(root: Path, content: bytes, expected_current: bytes) -> None:
    if read_regular_file(root / _ACTIVE_POINTER_NAME, expected_mode=0o600) != expected_current:
        raise ApplicationDeployError("content pointer changed before atomic activation")
    directory = open_directory_nofollow(root)
    temporary = f".active.{sha256_bytes(content)[:24]}.next"
    descriptor: int | None = None
    created = False
    try:
        try:
            descriptor = os.open(
                temporary,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory,
            )
        except FileExistsError:
            existing = read_regular_file_at(
                directory,
                temporary,
                label=temporary,
                expected_mode=0o600,
            )
            if existing != content:
                raise ApplicationDeployError("stale active-pointer staging file differs") from None
        else:
            created = True
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
        if (
            read_regular_file_at(
                directory,
                _ACTIVE_POINTER_NAME,
                label=_ACTIVE_POINTER_NAME,
                expected_mode=0o600,
            )
            != expected_current
        ):
            raise ApplicationDeployError("content pointer changed before pinned activation")
        metadata = os.stat(_ACTIVE_POINTER_NAME, dir_fd=directory, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise ApplicationDeployError("content pointer is not a private single-link file")
        os.replace(temporary, _ACTIVE_POINTER_NAME, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
        os.fsync(directory)
    except OSError as error:
        raise ApplicationDeployError(f"atomic content activation failed: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
        os.close(directory)


def _render_pointer(pointer: ContentPointer, database: str) -> bytes:
    return canonical_json_bytes(
        {
            "database": database,
            "releaseId": pointer.release_id,
            "stateHash": pointer.state_hash,
        }
    )


def _publish_role(
    root: Path,
    release_id: str,
    files: dict[str, bytes],
) -> str:
    relative = _release_target(release_id)
    destination = root.joinpath(*relative_parts(relative))
    if destination.exists():
        if read_tree_files(destination) != files:
            raise ApplicationDeployError("immutable application release id already has other bytes")
    else:
        publish_tree_directory(root / "releases", destination.name, files)
    return relative


def install_initial_application(
    target: ApplicationTarget,
    *,
    release_id: str,
    api_files: dict[str, bytes],
    web_files: dict[str, bytes],
) -> ActiveTargetState:
    """Install a test/local baseline; never replace an existing current symlink."""
    _ensure_target_roots(target)
    api_target = _publish_role(target.api_root, release_id, api_files)
    web_target = _publish_role(target.web_root, release_id, web_files)
    for root, current_target in ((target.api_root, api_target), (target.web_root, web_target)):
        directory = open_directory_nofollow(root)
        try:
            os.symlink(current_target, _CURRENT_NAME, dir_fd=directory)
            os.fsync(directory)
        except FileExistsError as error:
            raise ApplicationDeployError(
                f"initial application current already exists: {root}"
            ) from error
        finally:
            os.close(directory)
    return _active_state(target)


def _active_state(target: ApplicationTarget) -> ActiveTargetState:
    return ActiveTargetState(
        api_target=_read_current(target.api_root),
        web_target=_read_current(target.web_root),
        content_pointer=read_regular_file(
            target.content_root / _ACTIVE_POINTER_NAME,
            expected_mode=0o600,
        ),
    )


def _prepare_content(
    target: ApplicationTarget,
    manifest: ApplicationManifest,
    migration_files: dict[str, bytes],
    *,
    activated_at: str,
) -> tuple[bytes, ApplicationMigrationReport]:
    pointer = read_content_pointer(target.content_root)
    base = inspect_release_database(pointer.database_path)
    if (
        base.release.release_id != pointer.release_id
        or base.digest.state_hash != pointer.state_hash
    ):
        raise ApplicationDeployError("active content pointer differs from its database")
    relative = _content_relative(pointer, manifest.application_release_id)
    destination = target.content_root.joinpath(*relative_parts(relative))
    if not destination.exists():
        atomic_write_new(
            destination,
            read_regular_file(pointer.database_path, expected_mode=0o600),
            mode=0o600,
        )
    migrations = migrations_from_artifact_files(migration_files)
    try:
        with sqlite3.connect(destination) as connection:
            report = migrate_staging_connection(
                connection,
                manifest=manifest,
                activated_at=activated_at,
                migrations=migrations,
            )
    except sqlite3.Error as error:
        raise ApplicationDeployError(f"staging SQLite migration failed: {error}") from error
    for suffix in ("-journal", "-shm", "-wal"):
        if Path(str(destination) + suffix).exists():
            raise ApplicationDeployError(f"staging database left a forbidden sidecar: {suffix}")
    return _render_pointer(pointer, relative), report


def _prepare_target(
    target: ApplicationTarget,
    release: ParsedApplicationRelease,
    *,
    activated_at: str,
) -> tuple[PreparedTarget, dict[str, bytes], dict[str, bytes]]:
    _ensure_target_roots(target)
    previous = _active_state(target)
    api_files = parse_role_artifact(release.artifacts[API_ARTIFACT.name], API_ARTIFACT)
    web_files = parse_role_artifact(release.artifacts[WEB_ARTIFACT.name], WEB_ARTIFACT)
    migration_files = parse_role_artifact(
        release.artifacts[MIGRATION_ARTIFACT.name], MIGRATION_ARTIFACT
    )
    release_marker = canonical_json_bytes(
        {
            "applicationReleaseId": release.manifest.application_release_id,
            "gitCommit": release.manifest.git_commit,
            "packageSha256": release.sha256,
        }
    )
    api_files["APPLICATION-RELEASE.json"] = release_marker
    web_files["APPLICATION-RELEASE.json"] = release_marker
    api_target = _publish_role(
        target.api_root,
        release.manifest.application_release_id,
        api_files,
    )
    web_target = _publish_role(
        target.web_root,
        release.manifest.application_release_id,
        web_files,
    )
    content_pointer, migration = _prepare_content(
        target,
        release.manifest,
        migration_files,
        activated_at=activated_at,
    )
    return (
        PreparedTarget(
            target=target,
            previous=previous,
            api_target=api_target,
            web_target=web_target,
            content_pointer=content_pointer,
            migration=migration,
        ),
        api_files,
        web_files,
    )


def _verify_content(target: ApplicationTarget, manifest: ApplicationManifest) -> None:
    pointer = read_content_pointer(target.content_root)
    descriptor = open_regular_file_nofollow(pointer.database_path, expected_mode=0o600)
    try:
        with sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1", uri=True
        ) as connection:
            verify_public_database_connection(connection)
            if logical_state_hash(connection) != pointer.state_hash:
                raise ApplicationDeployError("active content pointer state differs during smoke")
            compatibility = connection.execute(
                """
                SELECT application_release_id
                FROM application_compatibility
                ORDER BY activated_at DESC, application_release_id DESC
                LIMIT 1
                """
            ).fetchone()
            if compatibility != (manifest.application_release_id,):
                raise ApplicationDeployError("active compatibility marker differs during smoke")
    finally:
        os.close(descriptor)


def _smoke_new(
    prepared: PreparedTarget,
    manifest: ApplicationManifest,
    api_files: dict[str, bytes],
    web_files: dict[str, bytes],
) -> None:
    if _read_current(prepared.target.api_root) != prepared.api_target:
        raise ApplicationDeployError("API current differs during application smoke")
    if _read_current(prepared.target.web_root) != prepared.web_target:
        raise ApplicationDeployError("web current differs during application smoke")
    if read_tree_files(prepared.target.api_root.joinpath(*relative_parts(prepared.api_target))) != (
        api_files
    ):
        raise ApplicationDeployError("deployed API tree differs from approved artifact")
    if read_tree_files(prepared.target.web_root.joinpath(*relative_parts(prepared.web_target))) != (
        web_files
    ):
        raise ApplicationDeployError("deployed web tree differs from approved artifact")
    _verify_content(prepared.target, manifest)


def _smoke_previous(prepared: PreparedTarget) -> None:
    state = _active_state(prepared.target)
    if state != prepared.previous:
        raise ApplicationRollbackError("rollback did not restore all prior active references")
    pointer = read_content_pointer(prepared.target.content_root)
    report = inspect_release_database(pointer.database_path)
    if (
        report.release.release_id != pointer.release_id
        or report.digest.state_hash != pointer.state_hash
    ):
        raise ApplicationRollbackError("rolled-back content pointer differs from its database")


def _inject(stage: str, fail_after: str | None) -> None:
    if stage == fail_after:
        raise SimulatedApplicationFailureError(f"simulated application failure after {stage}")


def _activate_target(
    prepared: PreparedTarget,
    manifest: ApplicationManifest,
    api_files: dict[str, bytes],
    web_files: dict[str, bytes],
    activation_order: list[str],
    *,
    fail_after: str | None,
) -> None:
    prefix = prepared.target.name
    _replace_content_pointer(
        prepared.target.content_root,
        prepared.content_pointer,
        prepared.previous.content_pointer,
    )
    activation_order.append(f"{prefix}-content")
    _inject(f"{prefix}-content", fail_after)
    _replace_current(prepared.target.api_root, prepared.api_target, prepared.previous.api_target)
    activation_order.append(f"{prefix}-api")
    _inject(f"{prefix}-api", fail_after)
    _replace_current(prepared.target.web_root, prepared.web_target, prepared.previous.web_target)
    activation_order.append(f"{prefix}-web")
    _inject(f"{prefix}-web", fail_after)
    _smoke_new(prepared, manifest, api_files, web_files)
    activation_order.append(f"{prefix}-smoke")
    _inject(f"{prefix}-smoke", fail_after)


def _rollback_target(prepared: PreparedTarget, rollback_order: list[str]) -> None:
    prefix = prepared.target.name
    current_web = _read_current(prepared.target.web_root)
    if current_web == prepared.web_target:
        _replace_current(
            prepared.target.web_root,
            prepared.previous.web_target,
            prepared.web_target,
        )
        rollback_order.append(f"{prefix}-web")
    current_api = _read_current(prepared.target.api_root)
    if current_api == prepared.api_target:
        _replace_current(
            prepared.target.api_root,
            prepared.previous.api_target,
            prepared.api_target,
        )
        rollback_order.append(f"{prefix}-api")
    current_content = read_regular_file(
        prepared.target.content_root / _ACTIVE_POINTER_NAME,
        expected_mode=0o600,
    )
    if current_content == prepared.content_pointer:
        _replace_content_pointer(
            prepared.target.content_root,
            prepared.previous.content_pointer,
            prepared.content_pointer,
        )
        rollback_order.append(f"{prefix}-content")
    _smoke_previous(prepared)
    rollback_order.append(f"{prefix}-smoke")


def _assert_migration_parity(source: PreparedTarget, production: PreparedTarget) -> None:
    source_report = source.migration
    production_report = production.migration
    if (
        source_report.content_release_id != production_report.content_release_id
        or source_report.state_hash != production_report.state_hash
        or source_report.schema_sha256 != production_report.schema_sha256
        or source_report.compatibility_sha256 != production_report.compatibility_sha256
        or source_report.applied_migrations != production_report.applied_migrations
    ):
        raise ApplicationDeployError("source and production migration reports differ")
    source_pointer = parse_content_pointer(
        source.target.content_root,
        source.content_pointer,
    )
    production_pointer = parse_content_pointer(
        production.target.content_root,
        production.content_pointer,
    )
    with (
        sqlite3.connect(source_pointer.database_path) as source_db,
        sqlite3.connect(production_pointer.database_path) as production_db,
    ):
        if database_digest(source_db) != database_digest(production_db):
            raise ApplicationDeployError(
                "source and production staging databases differ after migration"
            )


def deploy_application_release(
    package: bytes,
    *,
    approval: ManualApplicationApproval,
    source: ApplicationTarget,
    production: ApplicationTarget,
    work_root: Path,
    activated_at: str,
    prove_rollback: bool = True,
    fail_after: str | None = None,
) -> ApplicationDeployReport:
    """Stage, migrate and dependency-order activate both endpoints under one shared lock."""
    if source.name != "source" or production.name != "production":
        raise ApplicationDeployError("targets must use the explicit source/production order")
    _validate_target_topology(source, production, work_root)
    if fail_after is not None and fail_after not in _FAILURE_STAGES:
        raise ApplicationDeployError("unknown application failure-injection stage")
    release = parse_application_release(package)
    _validate_approval(release, approval)
    try:
        validate_utc_timestamp(activated_at, "activatedAt")
    except ValueError as error:
        raise ApplicationDeployError("application activation timestamp is invalid") from error
    try:
        lock = acquire_mutation_lock(work_root)
    except MutationLockBusyError as error:
        raise ApplicationDeployError("content/application mutation lock is busy") from error
    activation_order: list[str] = []
    rollback_order: list[str] = []
    source_prepared: PreparedTarget | None = None
    production_prepared: PreparedTarget | None = None
    try:
        source_prepared, source_api, source_web = _prepare_target(
            source,
            release,
            activated_at=activated_at,
        )
        production_prepared, production_api, production_web = _prepare_target(
            production,
            release,
            activated_at=activated_at,
        )
        _assert_migration_parity(source_prepared, production_prepared)
        try:
            _activate_target(
                source_prepared,
                release.manifest,
                source_api,
                source_web,
                activation_order,
                fail_after=fail_after,
            )
            _activate_target(
                production_prepared,
                release.manifest,
                production_api,
                production_web,
                activation_order,
                fail_after=fail_after,
            )
        except BaseException:
            try:
                if production_prepared is not None:
                    _rollback_target(production_prepared, rollback_order)
                if source_prepared is not None:
                    _rollback_target(source_prepared, rollback_order)
            except BaseException as rollback_error:
                raise ApplicationRollbackError(
                    "application failure was followed by an incomplete coordinated rollback"
                ) from rollback_error
            raise

        rollback_proven = False
        if prove_rollback:
            try:
                _rollback_target(production_prepared, rollback_order)
                _rollback_target(source_prepared, rollback_order)
            except BaseException as rollback_error:
                try:
                    _rollback_target(production_prepared, rollback_order)
                    _rollback_target(source_prepared, rollback_order)
                except BaseException as recovery_error:
                    raise ApplicationRollbackError(
                        "application rollback proof and recovery both failed"
                    ) from recovery_error
                raise ApplicationRollbackError(
                    "application rollback proof could not restore both endpoints"
                ) from rollback_error
            try:
                _activate_target(
                    source_prepared,
                    release.manifest,
                    source_api,
                    source_web,
                    activation_order,
                    fail_after=None,
                )
                _activate_target(
                    production_prepared,
                    release.manifest,
                    production_api,
                    production_web,
                    activation_order,
                    fail_after=None,
                )
            except BaseException:
                try:
                    _rollback_target(production_prepared, rollback_order)
                    _rollback_target(source_prepared, rollback_order)
                except BaseException as rollback_error:
                    raise ApplicationRollbackError(
                        "rollback proof reactivation failed and prior state was not restored"
                    ) from rollback_error
                raise
            rollback_proven = True

        return ApplicationDeployReport(
            application_release_id=release.manifest.application_release_id,
            approval_id=approval.approval_id,
            git_commit=release.manifest.git_commit,
            package_sha256=release.sha256,
            source_schema_sha256=source_prepared.migration.schema_sha256,
            production_schema_sha256=production_prepared.migration.schema_sha256,
            source_content_release_id=source_prepared.migration.content_release_id,
            production_content_release_id=production_prepared.migration.content_release_id,
            activation_order=tuple(activation_order),
            rollback_order=tuple(rollback_order),
            rollback_proven=rollback_proven,
            final_source=_active_state(source),
            final_production=_active_state(production),
        )
    except (ApplicationDeployError, SafeFilesystemError):
        raise
    except BaseException as error:
        raise ApplicationDeployError(f"application deployment failed closed: {error}") from error
    finally:
        release_mutation_lock(lock)


def deployment_report_document(report: ApplicationDeployReport) -> dict[str, object]:
    """Render a stable JSON-compatible final report without filesystem paths."""
    return {
        "activationOrder": list(report.activation_order),
        "approvalId": report.approval_id,
        "applicationReleaseId": report.application_release_id,
        "finalProduction": {
            "apiTarget": report.final_production.api_target,
            "contentPointerSha256": sha256_bytes(report.final_production.content_pointer),
            "webTarget": report.final_production.web_target,
        },
        "finalSource": {
            "apiTarget": report.final_source.api_target,
            "contentPointerSha256": sha256_bytes(report.final_source.content_pointer),
            "webTarget": report.final_source.web_target,
        },
        "gitCommit": report.git_commit,
        "packageSha256": report.package_sha256,
        "productionContentReleaseId": report.production_content_release_id,
        "productionSchemaSha256": report.production_schema_sha256,
        "rollbackOrder": list(report.rollback_order),
        "rollbackProven": report.rollback_proven,
        "sourceContentReleaseId": report.source_content_release_id,
        "sourceSchemaSha256": report.source_schema_sha256,
    }


__all__ = [
    "ActiveTargetState",
    "ApplicationApprovalError",
    "ApplicationDeployError",
    "ApplicationDeployReport",
    "ApplicationRollbackError",
    "ApplicationTarget",
    "ManualApplicationApproval",
    "SimulatedApplicationFailureError",
    "deploy_application_release",
    "deployment_report_document",
    "install_initial_application",
]
