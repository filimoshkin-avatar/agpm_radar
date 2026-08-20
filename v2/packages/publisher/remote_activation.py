"""Fail-closed Local Ru activation boundary for Radar V2 content deltas."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import stat
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, cast

from packages.delta.engine import apply_delta_to_staging, inspect_release_database, validate_delta
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.storage.content_pointer import parse_content_pointer
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
    read_regular_file,
)

MAX_REQUEST_BYTES: Final = 16 * 1024 * 1024
REQUEST_FIELDS: Final = frozenset(
    {"action", "delta", "expectedCurrentPointerSha256", "requestId", "rollbackPointer"}
)
ALLOWED_ACTIONS: Final = frozenset({"publish", "rollback", "status"})


class RemoteActivationError(RuntimeError):
    """A remote activation request failed before a proven success."""


@dataclass(frozen=True, slots=True)
class RemoteActivationResult:
    """Machine result returned through the forced-command transport."""

    request_id: str
    status: str
    release_id: str
    state_hash: str
    previous_release_id: str | None
    previous_state_hash: str | None
    database_sha256: str
    pointer_sha256: str
    loopback_verified: bool


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _request_token(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise RemoteActivationError(f"{field} is invalid")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    if any(character not in allowed for character in value):
        raise RemoteActivationError(f"{field} contains forbidden characters")
    return value


def read_request(stream: object) -> JsonObject:
    """Read one bounded canonical JSON request from a binary stream."""
    reader = getattr(stream, "read", None)
    if reader is None:
        raise RemoteActivationError("request stream is not readable")
    content = cast(bytes, reader(MAX_REQUEST_BYTES + 1))
    if len(content) > MAX_REQUEST_BYTES:
        raise RemoteActivationError("request exceeds size limit")
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemoteActivationError(f"request JSON is invalid: {error}") from error
    if not isinstance(document, dict) or set(document) != REQUEST_FIELDS:
        raise RemoteActivationError("request has unknown or missing fields")
    action = document.get("action")
    if action not in ALLOWED_ACTIONS:
        raise RemoteActivationError("request action is not allowed")
    _request_token(document.get("requestId"), "requestId")
    pointer_sha = document.get("expectedCurrentPointerSha256")
    if not isinstance(pointer_sha, str) or len(pointer_sha) != 64:
        raise RemoteActivationError("expectedCurrentPointerSha256 is invalid")
    if any(character not in "0123456789abcdef" for character in pointer_sha):
        raise RemoteActivationError("expectedCurrentPointerSha256 is invalid")
    if action == "publish":
        delta = document.get("delta")
        if not isinstance(delta, dict):
            raise RemoteActivationError("publish request delta is missing")
        validate_delta(delta)
        if document.get("rollbackPointer") is not None:
            raise RemoteActivationError("publish request must not carry a rollback pointer")
    elif action == "rollback":
        if document.get("delta") is not None:
            raise RemoteActivationError("rollback request must not carry a delta")
        pointer = document.get("rollbackPointer")
        if not isinstance(pointer, dict):
            raise RemoteActivationError("rollback request pointer is missing")
        if set(pointer) != {"database", "releaseId", "stateHash"}:
            raise RemoteActivationError("rollback pointer has unknown or missing fields")
    elif document.get("delta") is not None or document.get("rollbackPointer") is not None:
        raise RemoteActivationError("status request must not carry mutation data")
    return cast(JsonObject, document)


def _pointer_bytes(release_id: str, state_hash: str) -> bytes:
    token = hashlib.sha256(release_id.encode("utf-8")).hexdigest()[:32]
    return canonical_json_line(
        {
            "database": f"releases/{token}.sqlite",
            "releaseId": release_id,
            "stateHash": state_hash,
        }
    )


def _pointer_metadata(path: Path, *, uid: int, gid: int) -> bytes:
    content = read_regular_file(path, expected_mode=0o600)
    metadata = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or metadata.st_nlink != 1
    ):
        raise RemoteActivationError("active pointer security metadata is invalid")
    return content


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise RemoteActivationError("short pointer write")
        remaining = remaining[written:]


def replace_pointer(root: Path, content: bytes, *, uid: int, gid: int, expected: bytes) -> None:
    """Atomically replace active.json while preserving exact ownership and durability."""
    if _pointer_metadata(root / "active.json", uid=uid, gid=gid) != expected:
        raise RemoteActivationError("active pointer changed before activation")
    directory = open_directory_nofollow(root)
    temporary = f".active.{_sha256(content)[:24]}.next"
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        created = True
        _write_all(descriptor, content)
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, uid, gid)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if metadata.st_uid != uid or metadata.st_gid != gid or metadata.st_nlink != 1:
            raise RemoteActivationError("staged pointer metadata is invalid")
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, "active.json", src_dir_fd=directory, dst_dir_fd=directory)
        created = False
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
        os.close(directory)
    if _pointer_metadata(root / "active.json", uid=uid, gid=gid) != content:
        raise RemoteActivationError("activated pointer bytes or metadata differ")


def _health(url: str, release_id: str, state_hash: str) -> None:
    last_error = "health did not return expected markers"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310
                body = response.read(1024 * 1024)
                status = response.status
            if status != 200:
                last_error = f"health returned HTTP {status}"
            else:
                document = json.loads(body)
                if not isinstance(document, dict):
                    last_error = "health response is not an object"
                else:
                    observed_release = document.get("contentReleaseId", document.get("releaseId"))
                    observed_state = document.get("databaseStateHash", document.get("stateHash"))
                    if observed_release == release_id and observed_state == state_hash:
                        return
                    last_error = "health release/state markers differ"
        except (OSError, urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError) as error:
            last_error = f"health request failed: {error}"
        time.sleep(0.2)
    raise RemoteActivationError(last_error)


def _install_database(target: Path, source: Path, *, uid: int, gid: int) -> str:
    content = read_regular_file(source, expected_mode=0o600)
    if target.exists():
        existing = read_regular_file(target, expected_mode=0o600)
        if existing != content:
            raise RemoteActivationError("release path already contains different bytes")
    else:
        directory = open_directory_nofollow(target.parent)
        temporary = f".{target.name}.{_sha256(content)[:24]}.next"
        descriptor: int | None = None
        created = False
        try:
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            created = True
            _write_all(descriptor, content)
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, uid, gid)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            if target.exists():
                raise RemoteActivationError("release path appeared during installation")
            os.rename(temporary, target.name, src_dir_fd=directory, dst_dir_fd=directory)
            created = False
            os.fsync(directory)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if created:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
            os.close(directory)
    metadata = os.stat(target, follow_symlinks=False)
    if metadata.st_uid != uid or metadata.st_gid != gid or metadata.st_nlink != 1:
        raise RemoteActivationError("release database metadata is invalid")
    return _sha256(content)


def activate_request(
    request: JsonObject,
    *,
    content_root: Path,
    incoming_root: Path,
    audit_root: Path,
    mutation_root: Path,
    api_user: str = "radar-v2-api",
    loopback_url: str = "http://127.0.0.1:8765/api/health",
    public_url: str = "https://radar.agpm.space/api/health",
    failure_stage: str | None = None,
) -> RemoteActivationResult:
    """Quarantine, stage, activate, verify and roll back one exact request."""
    identity = pwd.getpwnam(api_user)
    uid, gid = identity.pw_uid, identity.pw_gid
    request_id = _request_token(request["requestId"], "requestId")
    ensure_private_directory(incoming_root)
    ensure_private_directory(audit_root)
    ensure_private_directory(mutation_root)
    reconciliation_marker = audit_root / "NEEDS_RECONCILIATION"
    if reconciliation_marker.exists():
        read_regular_file(reconciliation_marker, expected_mode=0o600)
        raise RemoteActivationError("publisher is blocked by NEEDS_RECONCILIATION")
    request_bytes = canonical_json_line(request)
    quarantine = incoming_root / f"{request_id}.json"
    if quarantine.exists():
        if read_regular_file(quarantine, expected_mode=0o600) != request_bytes:
            raise RemoteActivationError("request id already exists with different bytes")
    else:
        atomic_write_new(quarantine, request_bytes, mode=0o600)
    pointer_path = content_root / "active.json"
    previous_bytes = _pointer_metadata(pointer_path, uid=uid, gid=gid)
    if _sha256(previous_bytes) != request["expectedCurrentPointerSha256"]:
        raise RemoteActivationError("active pointer SHA-256 differs from request fence")
    previous = parse_content_pointer(content_root, previous_bytes)
    if request["action"] == "status":
        report = inspect_release_database(previous.database_path)
        return RemoteActivationResult(
            request_id=request_id,
            status="status",
            release_id=previous.release_id,
            state_hash=previous.state_hash,
            previous_release_id=None,
            previous_state_hash=None,
            database_sha256=report.file_sha256,
            pointer_sha256=_sha256(previous_bytes),
            loopback_verified=False,
        )
    if request["action"] == "rollback":
        rollback_bytes = canonical_json_line(cast(dict[str, object], request["rollbackPointer"]))
        rollback = parse_content_pointer(content_root, rollback_bytes)
        report = inspect_release_database(rollback.database_path)
        if (
            report.release.release_id != rollback.release_id
            or report.digest.state_hash != rollback.state_hash
        ):
            raise RemoteActivationError("rollback pointer differs from preserved release")
        rollback_lock: int | None = None
        try:
            rollback_lock = acquire_mutation_lock(mutation_root)
            replace_pointer(
                content_root,
                rollback_bytes,
                uid=uid,
                gid=gid,
                expected=previous_bytes,
            )
            _health(loopback_url, rollback.release_id, rollback.state_hash)
            _health(public_url, rollback.release_id, rollback.state_hash)
        except (MutationLockBusyError, SafeFilesystemError) as error:
            raise RemoteActivationError(str(error)) from error
        finally:
            if rollback_lock is not None:
                release_mutation_lock(rollback_lock)
        return RemoteActivationResult(
            request_id=request_id,
            status="rolled_back",
            release_id=rollback.release_id,
            state_hash=rollback.state_hash,
            previous_release_id=previous.release_id,
            previous_state_hash=previous.state_hash,
            database_sha256=report.file_sha256,
            pointer_sha256=_sha256(rollback_bytes),
            loopback_verified=True,
        )
    delta = validate_delta(cast(dict[str, object], request["delta"]))
    if (previous.release_id, previous.state_hash) != (
        delta["baseReleaseId"],
        delta["beforeStateHash"],
    ):
        raise RemoteActivationError("remote base release/state differs from delta")
    lock: int | None = None
    activated = False
    target_pointer = _pointer_bytes(
        cast(str, delta["releaseId"]), cast(str, delta["afterStateHash"])
    )
    staging = incoming_root / f"{request_id}.staging.sqlite"
    target = content_root / cast(str, json.loads(target_pointer)["database"])
    try:
        lock = acquire_mutation_lock(mutation_root)
        if not target.exists():
            if staging.exists():
                staged = inspect_release_database(staging)
                if (
                    staged.release.release_id != delta["releaseId"]
                    or staged.digest.state_hash != delta["afterStateHash"]
                ):
                    raise RemoteActivationError("preserved staging release differs from delta")
            else:
                apply_delta_to_staging(previous.database_path, staging, delta)
            database_sha = _install_database(target, staging, uid=uid, gid=gid)
        else:
            report = inspect_release_database(target)
            if (
                report.release.release_id != delta["releaseId"]
                or report.digest.state_hash != delta["afterStateHash"]
            ):
                raise RemoteActivationError("existing target release differs from delta")
            database_sha = report.file_sha256
        replace_pointer(content_root, target_pointer, uid=uid, gid=gid, expected=previous_bytes)
        activated = True
        if failure_stage == "loopback":
            raise RemoteActivationError("injected loopback verification failure")
        _health(loopback_url, cast(str, delta["releaseId"]), cast(str, delta["afterStateHash"]))
        if failure_stage == "public":
            raise RemoteActivationError("injected public verification failure")
        _health(public_url, cast(str, delta["releaseId"]), cast(str, delta["afterStateHash"]))
        result = RemoteActivationResult(
            request_id=request_id,
            status="published",
            release_id=cast(str, delta["releaseId"]),
            state_hash=cast(str, delta["afterStateHash"]),
            previous_release_id=previous.release_id,
            previous_state_hash=previous.state_hash,
            database_sha256=database_sha,
            pointer_sha256=_sha256(target_pointer),
            loopback_verified=True,
        )
    except (MutationLockBusyError, SafeFilesystemError) as error:
        raise RemoteActivationError(str(error)) from error
    except BaseException as publication_error:
        if activated:
            try:
                replace_pointer(
                    content_root,
                    previous_bytes,
                    uid=uid,
                    gid=gid,
                    expected=target_pointer,
                )
                _health(loopback_url, previous.release_id, previous.state_hash)
                _health(public_url, previous.release_id, previous.state_hash)
            except BaseException as rollback_error:
                marker = canonical_json_line(
                    {
                        "publicationError": str(publication_error),
                        "requestId": request_id,
                        "rollbackError": str(rollback_error),
                        "status": "NEEDS_RECONCILIATION",
                    }
                )
                if not reconciliation_marker.exists():
                    atomic_write_new(reconciliation_marker, marker, mode=0o600)
                raise RemoteActivationError(
                    "rollback could not be proven; publisher is blocked by NEEDS_RECONCILIATION"
                ) from rollback_error
        raise publication_error
    finally:
        if lock is not None:
            release_mutation_lock(lock)
    audit_path = audit_root / f"{request_id}.result.json"
    result_bytes = canonical_json_line(asdict(result))
    if audit_path.exists():
        if read_regular_file(audit_path, expected_mode=0o600) != result_bytes:
            raise RemoteActivationError("saved result differs from successful replay")
    else:
        atomic_write_new(audit_path, result_bytes, mode=0o600)
    return result


__all__ = [
    "MAX_REQUEST_BYTES",
    "RemoteActivationError",
    "RemoteActivationResult",
    "activate_request",
    "read_request",
    "replace_pointer",
]
