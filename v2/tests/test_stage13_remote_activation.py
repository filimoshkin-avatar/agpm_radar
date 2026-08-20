"""Stage 13 forced-command request and atomic pointer security gates."""

from __future__ import annotations

import hashlib
import io
import json
import os
import pwd
import stat
from pathlib import Path

import pytest
from packages.publisher.remote_activation import (
    MAX_REQUEST_BYTES,
    RemoteActivationError,
    read_request,
    replace_pointer,
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _status_request() -> bytes:
    return json.dumps(
        {
            "action": "status",
            "delta": None,
            "expectedCurrentPointerSha256": "a" * 64,
            "requestId": "stage13-status-0001",
            "rollbackPointer": None,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def test_forced_command_request_is_closed_and_bounded() -> None:
    request = read_request(io.BytesIO(_status_request()))
    assert request["action"] == "status"

    unknown = json.loads(_status_request())
    unknown["shell"] = "id"
    with pytest.raises(RemoteActivationError, match="unknown or missing"):
        read_request(io.BytesIO(json.dumps(unknown).encode()))

    with pytest.raises(RemoteActivationError, match="size limit"):
        read_request(io.BytesIO(b"x" * (MAX_REQUEST_BYTES + 1)))


def test_status_request_cannot_carry_delta() -> None:
    request = json.loads(_status_request())
    request["delta"] = {}
    with pytest.raises(RemoteActivationError, match="must not carry"):
        read_request(io.BytesIO(json.dumps(request).encode()))


def test_rollback_request_has_closed_pointer_contract() -> None:
    request = json.loads(_status_request())
    request["action"] = "rollback"
    request["rollbackPointer"] = {
        "database": "releases/old.sqlite",
        "releaseId": "old",
        "stateHash": "b" * 64,
    }
    assert read_request(io.BytesIO(json.dumps(request).encode()))["action"] == "rollback"
    request["rollbackPointer"]["command"] = "id"
    with pytest.raises(RemoteActivationError, match="unknown or missing"):
        read_request(io.BytesIO(json.dumps(request).encode()))


def test_atomic_pointer_preserves_exact_security_metadata(tmp_path: Path) -> None:
    identity = pwd.getpwuid(os.getuid())
    previous = (
        b'{"database":"releases/old.sqlite","releaseId":"old","stateHash":"' + (b"a" * 64) + b'"}\n'
    )
    target = (
        b'{"database":"releases/new.sqlite","releaseId":"new","stateHash":"' + (b"b" * 64) + b'"}\n'
    )
    pointer = tmp_path / "active.json"
    pointer.write_bytes(previous)
    pointer.chmod(0o600)

    replace_pointer(
        tmp_path,
        target,
        uid=identity.pw_uid,
        gid=identity.pw_gid,
        expected=previous,
    )

    metadata = pointer.stat(follow_symlinks=False)
    assert pointer.read_bytes() == target
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_uid == identity.pw_uid
    assert metadata.st_gid == identity.pw_gid
    assert metadata.st_nlink == 1
    assert not tuple(tmp_path.glob(".active.*.next"))


def test_atomic_pointer_rejects_stale_fence_and_bad_owner(tmp_path: Path) -> None:
    identity = pwd.getpwuid(os.getuid())
    pointer = tmp_path / "active.json"
    pointer.write_bytes(b"current\n")
    pointer.chmod(0o600)

    with pytest.raises(RemoteActivationError, match="changed before activation"):
        replace_pointer(
            tmp_path,
            b"target\n",
            uid=identity.pw_uid,
            gid=identity.pw_gid,
            expected=b"stale\n",
        )
    assert _sha256(pointer.read_bytes()) == _sha256(b"current\n")
