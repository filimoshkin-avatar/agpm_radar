"""One host-local lock shared by content publication and application migration."""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path
from typing import Final

from packages.storage.safe_files import ensure_private_directory

MUTATION_LOCK_NAME: Final = "radar-mutation.lock"


class MutationLockError(RuntimeError):
    """The shared application/content mutation lock cannot be acquired safely."""


class MutationLockBusyError(MutationLockError):
    """Another content publisher or application deploy owns the mutation lock."""


def acquire_mutation_lock(work_root: Path) -> int:
    """Return an exclusively locked descriptor or fail without waiting."""
    ensure_private_directory(work_root)
    path = work_root / MUTATION_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise MutationLockError("mutation lock is not a private single-link file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BlockingIOError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise MutationLockBusyError("radar mutation lock is busy") from error
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def release_mutation_lock(descriptor: int) -> None:
    """Release and close a descriptor returned by :func:`acquire_mutation_lock`."""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


__all__ = [
    "MUTATION_LOCK_NAME",
    "MutationLockBusyError",
    "MutationLockError",
    "acquire_mutation_lock",
    "release_mutation_lock",
]
