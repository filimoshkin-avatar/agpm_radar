#!/usr/bin/env python3
"""Bounded, reference-aware retention for immutable Radar V2 SQLite releases."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

LOCK_NAME: Final = "radar-mutation.lock"
POINTER_FIELDS: Final = frozenset({"database", "releaseId", "stateHash"})


class RetentionError(RuntimeError):
    """The retention pass cannot prove that deletion is safe."""


@dataclass(frozen=True, slots=True)
class Candidate:
    path: str
    bytes: int
    device: int
    inode: int
    mtime_ns: int
    category: str


@dataclass(frozen=True, slots=True)
class Plan:
    mode: str
    apply: bool
    retention_days: int
    minimum_releases: int
    cutoff_epoch: int
    active_database: str
    protected_databases: tuple[str, ...]
    candidates: tuple[Candidate, ...]


def _load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RetentionError(f"cannot read valid JSON from {path}: {error}") from error
    if not isinstance(value, dict):
        raise RetentionError(f"JSON root is not an object: {path}")
    return value


def _pointer_database(path: Path) -> str:
    value = _load_json(path)
    if set(value) != POINTER_FIELDS:
        raise RetentionError(f"pointer has unknown or missing fields: {path}")
    database = value["database"]
    release_id = value["releaseId"]
    state_hash = value["stateHash"]
    if (
        not isinstance(database, str)
        or not database.startswith("releases/")
        or Path(database).name != database.removeprefix("releases/")
        or not database.endswith(".sqlite")
    ):
        raise RetentionError(f"pointer database is unsafe: {path}")
    if not isinstance(release_id, str) or not release_id:
        raise RetentionError(f"pointer release id is invalid: {path}")
    if (
        not isinstance(state_hash, str)
        or len(state_hash) != 64
        or any(character not in "0123456789abcdef" for character in state_hash)
    ):
        raise RetentionError(f"pointer state hash is invalid: {path}")
    return Path(database).name


def _regular_metadata(path: Path) -> os.stat_result:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise RetentionError(f"expected file is missing: {path}") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RetentionError(f"file is not a regular single-link artifact: {path}")
    return metadata


def _sqlite_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise RetentionError(f"release directory is missing: {root}")
    result: list[Path] = []
    for path in root.iterdir():
        if path.name.endswith(".sqlite"):
            _regular_metadata(path)
            result.append(path)
    return sorted(result, key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)


def _verify_active(root: Path) -> str:
    pointer = root / "active.json"
    active_name = _pointer_database(pointer)
    database = root / "releases" / active_name
    _regular_metadata(database)
    uri = f"file:{database.resolve()}?mode=ro&immutable=1"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        row = connection.execute(
            "SELECT release_id, after_state_hash FROM content_releases "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as error:
        raise RetentionError(f"active SQLite verification failed: {database}: {error}") from error
    finally:
        if connection is not None:
            connection.close()
    pointer_value = _load_json(pointer)
    if quick_check != ("ok",) or row != (
        pointer_value["releaseId"],
        pointer_value["stateHash"],
    ):
        raise RetentionError("active pointer and SQLite identity differ")
    return active_name


def _release_identity(path: Path) -> tuple[str, str]:
    _regular_metadata(path)
    uri = f"file:{path.resolve()}?mode=ro&immutable=1"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        row = connection.execute(
            "SELECT release_id, after_state_hash FROM content_releases "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as error:
        raise RetentionError(f"release SQLite verification failed: {path}: {error}") from error
    finally:
        if connection is not None:
            connection.close()
    if (
        quick_check != ("ok",)
        or not isinstance(row, tuple)
        or len(row) != 2
        or not all(isinstance(value, str) and value for value in row)
    ):
        raise RetentionError(f"release SQLite identity is invalid: {path}")
    return row


def _hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validated_source_result(
    result_path: Path,
    request_path: Path,
    candidate_path: Path,
    candidate_id: str,
) -> bool:
    if not result_path.exists():
        return False
    result = _load_json(result_path)
    request = _load_json(request_path)
    delta = request.get("delta")
    if not isinstance(delta, dict):
        raise RetentionError(f"completed source request has no delta: {request_path}")
    release_id = delta.get("releaseId")
    state_hash = delta.get("afterStateHash")
    valid = (
        delta.get("candidateId") == candidate_id
        and result.get("candidateId") == candidate_id
        and result.get("status") == "published"
        and result.get("publicationSucceeded") is True
        and result.get("publishingBlocked") is False
        and result.get("exitCode") == 0
        and result.get("error") is None
        and result.get("releaseId") == release_id
        and result.get("activeReleaseId") == release_id
        and result.get("sourceStateHash") == state_hash
        and result.get("productionStateHash") == state_hash
        and _hash(state_hash)
    )
    if not valid:
        raise RetentionError(f"source result is not a proven success: {result_path}")
    if candidate_path.exists() and _release_identity(candidate_path) != (release_id, state_hash):
        raise RetentionError(f"source result differs from candidate database: {result_path}")
    return True


def _validated_remote_result(
    result_path: Path,
    request_path: Path,
    request_id: str,
) -> tuple[str, str] | None:
    if not result_path.exists():
        return None
    result = _load_json(result_path)
    request = _load_json(request_path)
    delta = request.get("delta")
    if request.get("action") != "publish" or not isinstance(delta, dict):
        raise RetentionError(f"remote success result is not bound to publish: {result_path}")
    release_id = delta.get("releaseId")
    state_hash = delta.get("afterStateHash")
    expected_fields = {
        "database_sha256",
        "loopback_verified",
        "pointer_sha256",
        "previous_release_id",
        "previous_state_hash",
        "release_id",
        "request_id",
        "state_hash",
        "status",
    }
    valid = (
        set(result) == expected_fields
        and request.get("requestId") == request_id
        and result.get("request_id") == request_id
        and result.get("status") == "published"
        and result.get("loopback_verified") is True
        and result.get("release_id") == release_id
        and result.get("state_hash") == state_hash
        and _hash(result.get("database_sha256"))
        and _hash(result.get("pointer_sha256"))
        and _hash(state_hash)
    )
    if not valid or not isinstance(release_id, str) or not isinstance(state_hash, str):
        raise RetentionError(f"remote result is not a proven success: {result_path}")
    return release_id, state_hash


def _candidate(path: Path, category: str) -> Candidate:
    metadata = _regular_metadata(path)
    return Candidate(
        path=str(path),
        bytes=metadata.st_size,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mtime_ns=metadata.st_mtime_ns,
        category=category,
    )


def _old_candidates(
    files: list[Path],
    *,
    protected: set[str],
    cutoff_ns: int,
    category: str,
) -> list[Candidate]:
    return [
        _candidate(path, category)
        for path in files
        if path.name not in protected and path.stat().st_mtime_ns < cutoff_ns
    ]


def _target_name(release_id: object) -> str:
    if not isinstance(release_id, str) or not release_id:
        raise RetentionError("unresolved request release id is invalid")
    return f"{hashlib.sha256(release_id.encode()).hexdigest()[:32]}.sqlite"


def _source_plan(
    *,
    source_root: Path,
    publisher_root: Path,
    retention_days: int,
    minimum_releases: int,
    cutoff_ns: int,
    apply: bool,
) -> Plan:
    active = _verify_active(source_root)
    source_files = _sqlite_files(source_root / "releases")
    publisher_files = _sqlite_files(publisher_root / "releases")
    protected_source = {active}
    protected_source.update(path.name for path in source_files[:minimum_releases])
    protected_publisher = {path.name for path in publisher_files[:minimum_releases]}

    for pointer in source_root.glob("active*.json"):
        protected_source.add(_pointer_database(pointer))

    requests = publisher_root / "requests"
    results = publisher_root / "results"
    if not requests.is_dir() or not results.is_dir():
        raise RetentionError("publisher requests/results directories are missing")
    for request in requests.glob("*.json"):
        if request.name.endswith(".base-pointer.json"):
            continue
        candidate_id = request.stem
        candidate_path = publisher_root / "releases" / f"{candidate_id}.sqlite"
        if _validated_source_result(
            results / f"{candidate_id}.json",
            request,
            candidate_path,
            candidate_id,
        ):
            continue
        protected_publisher.add(f"{candidate_id}.sqlite")
        base_pointer = requests / f"{candidate_id}.base-pointer.json"
        if base_pointer.exists():
            protected_source.add(_pointer_database(base_pointer))

    candidates = _old_candidates(
        source_files,
        protected=protected_source,
        cutoff_ns=cutoff_ns,
        category="source_release",
    )
    candidates.extend(
        _old_candidates(
            publisher_files,
            protected=protected_publisher,
            cutoff_ns=cutoff_ns,
            category="publisher_release",
        )
    )
    return Plan(
        mode="source",
        apply=apply,
        retention_days=retention_days,
        minimum_releases=minimum_releases,
        cutoff_epoch=cutoff_ns // 1_000_000_000,
        active_database=active,
        protected_databases=tuple(sorted(protected_source | protected_publisher)),
        candidates=tuple(sorted(candidates, key=lambda item: item.path)),
    )


def _remote_plan(
    *,
    content_root: Path,
    incoming_root: Path,
    audit_root: Path,
    retention_days: int,
    minimum_releases: int,
    cutoff_ns: int,
    apply: bool,
) -> Plan:
    reconciliation = audit_root / "NEEDS_RECONCILIATION"
    if reconciliation.exists():
        raise RetentionError(f"reconciliation marker blocks retention: {reconciliation}")
    active = _verify_active(content_root)
    releases = _sqlite_files(content_root / "releases")
    protected = {active}
    protected.update(path.name for path in releases[:minimum_releases])
    if not incoming_root.is_dir() or not audit_root.is_dir():
        raise RetentionError("remote incoming/audit directories are missing")

    completed: dict[str, tuple[str, str]] = {}
    for request in incoming_root.glob("*.json"):
        if request.name.endswith(".base-pointer.json"):
            continue
        request_id = request.stem
        result = _validated_remote_result(
            audit_root / f"{request_id}.result.json",
            request,
            request_id,
        )
        if result is not None:
            completed[request_id] = result
            continue
        value = _load_json(request)
        action = value.get("action")
        if action == "publish":
            base_pointer = incoming_root / f"{request_id}.base-pointer.json"
            if base_pointer.exists():
                protected.add(_pointer_database(base_pointer))
            delta = value.get("delta")
            if not isinstance(delta, dict):
                raise RetentionError(f"unresolved publish request has no delta: {request}")
            protected.add(_target_name(delta.get("releaseId")))
        elif action == "rollback":
            rollback = value.get("rollbackPointer")
            if not isinstance(rollback, dict):
                raise RetentionError(f"unresolved rollback request has no pointer: {request}")
            database = rollback.get("database")
            if (
                not isinstance(database, str)
                or not database.startswith("releases/")
                or Path(database).name != database.removeprefix("releases/")
            ):
                raise RetentionError(f"unresolved rollback pointer is unsafe: {request}")
            protected.add(Path(database).name)
        elif action != "status":
            raise RetentionError(f"unresolved request action is unknown: {request}")

    candidates = _old_candidates(
        releases,
        protected=protected,
        cutoff_ns=cutoff_ns,
        category="production_release",
    )
    for staging in incoming_root.glob("*.staging.sqlite"):
        metadata = _regular_metadata(staging)
        if metadata.st_mtime_ns >= cutoff_ns:
            continue
        request_id = staging.name.removesuffix(".staging.sqlite")
        if request_id in completed:
            if _release_identity(staging) != completed[request_id]:
                raise RetentionError(f"completed staging differs from audit result: {staging}")
            candidates.append(_candidate(staging, "completed_staging"))

    return Plan(
        mode="remote",
        apply=apply,
        retention_days=retention_days,
        minimum_releases=minimum_releases,
        cutoff_epoch=cutoff_ns // 1_000_000_000,
        active_database=active,
        protected_databases=tuple(sorted(protected)),
        candidates=tuple(sorted(candidates, key=lambda item: item.path)),
    )


def _acquire_lock(root: Path) -> int:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        root / LOCK_NAME,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise RetentionError("mutation lock is not a regular single-link file")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise RetentionError("radar mutation lock is busy") from error
    return descriptor


def _unlink(candidate: Candidate) -> None:
    path = Path(candidate.path)
    if path.name in {"", ".", ".."} or path.parent == path:
        raise RetentionError(f"candidate path is unsafe: {path}")
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_dev != candidate.device
            or metadata.st_ino != candidate.inode
            or metadata.st_size != candidate.bytes
            or metadata.st_mtime_ns != candidate.mtime_ns
        ):
            raise RetentionError(f"candidate changed after planning: {path}")
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("--minimum-releases", type=int, default=7)
    parser.add_argument("--apply", action="store_true")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    source = subparsers.add_parser("source")
    source.add_argument("--source-root", type=Path, required=True)
    source.add_argument("--publisher-root", type=Path, required=True)
    remote = subparsers.add_parser("remote")
    remote.add_argument("--content-root", type=Path, required=True)
    remote.add_argument("--incoming-root", type=Path, required=True)
    remote.add_argument("--audit-root", type=Path, required=True)
    remote.add_argument("--mutation-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.retention_days < 1 or arguments.minimum_releases < 2:
        raise RetentionError("retention days must be >= 1 and minimum releases >= 2")
    cutoff_ns = time.time_ns() - arguments.retention_days * 86_400 * 1_000_000_000
    lock_root = arguments.publisher_root if arguments.mode == "source" else arguments.mutation_root
    lock = _acquire_lock(lock_root)
    try:
        if arguments.mode == "source":
            plan = _source_plan(
                source_root=arguments.source_root,
                publisher_root=arguments.publisher_root,
                retention_days=arguments.retention_days,
                minimum_releases=arguments.minimum_releases,
                cutoff_ns=cutoff_ns,
                apply=arguments.apply,
            )
            active_root = arguments.source_root
        else:
            plan = _remote_plan(
                content_root=arguments.content_root,
                incoming_root=arguments.incoming_root,
                audit_root=arguments.audit_root,
                retention_days=arguments.retention_days,
                minimum_releases=arguments.minimum_releases,
                cutoff_ns=cutoff_ns,
                apply=arguments.apply,
            )
            active_root = arguments.content_root
        if arguments.apply:
            for candidate in plan.candidates:
                _unlink(candidate)
            if _verify_active(active_root) != plan.active_database:
                raise RetentionError("active database changed during retention")
        document = asdict(plan)
        document["candidate_bytes"] = sum(item.bytes for item in plan.candidates)
        document["candidate_count"] = len(plan.candidates)
        print(json.dumps(document, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RetentionError as error:
        print(f"retention blocked: {error}", file=sys.stderr)
        raise SystemExit(1) from error
