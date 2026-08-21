"""Race-safe private filesystem primitives for immutable V2 artifacts."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Final

_DIRECTORY_FLAGS: Final = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_RENAME_NOREPLACE: Final = 1
_READ_CHUNK: Final = 1024 * 1024


class SafeFilesystemError(RuntimeError):
    """A path, file type, permission or atomic-write invariant was violated."""


class PathEscapeError(SafeFilesystemError):
    """A caller attempted to leave its assigned filesystem capability."""


class ArtifactExistsError(SafeFilesystemError):
    """An immutable target already exists and was not replaced."""


def _simple_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise PathEscapeError(f"unsafe filesystem component: {name!r}")
    return name


def relative_parts(relative: str | PurePosixPath) -> tuple[str, ...]:
    """Validate a portable relative path and return its components."""
    text = str(relative)
    path = PurePosixPath(text)
    if (
        not text
        or len(text) > 512
        or path.is_absolute()
        or "\\" in text
        or "\x00" in text
        or any(part in {"", ".", ".."} for part in path.parts)
        or PurePosixPath(*path.parts).as_posix() != text
    ):
        raise PathEscapeError(f"unsafe relative path: {text!r}")
    return tuple(_simple_name(part) for part in path.parts)


def open_directory_nofollow(path: Path) -> int:
    """Open every path component with ``O_NOFOLLOW`` and return the pinned directory FD."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    if not parts or parts[0] != os.sep:
        raise PathEscapeError(f"cannot normalize directory path: {path}")
    descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        for component in parts[1:]:
            name = _simple_name(component)
            next_descriptor = os.open(
                name,
                _DIRECTORY_FLAGS | _NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise SafeFilesystemError(f"not a directory: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_private_directory(descriptor: int, label: str) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise SafeFilesystemError(f"not a directory: {label}")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SafeFilesystemError(f"directory permissions are broader than private: {label}")
    return metadata


def create_private_directory(path: Path, mode: int = 0o700) -> None:
    """Atomically reserve a new private directory without following parent symlinks."""
    if mode & 0o077:
        raise SafeFilesystemError("private directory mode grants group/other access")
    parent_descriptor = open_directory_nofollow(path.parent)
    try:
        _simple_name(path.name)
        os.mkdir(path.name, mode=mode, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except FileExistsError as error:
        raise ArtifactExistsError(f"immutable directory already exists: {path}") from error
    finally:
        os.close(parent_descriptor)


def ensure_private_directory(path: Path, mode: int = 0o700) -> None:
    """Create a private leaf directory, or validate the existing leaf without symlinks."""
    try:
        create_private_directory(path, mode)
    except ArtifactExistsError:
        descriptor = open_directory_nofollow(path)
        try:
            _require_private_directory(descriptor, str(path))
        finally:
            os.close(descriptor)


def _rename_noreplace(
    source_parent: int,
    source_name: str,
    target_parent: int,
    target_name: str,
) -> None:
    """Use Linux renameat2 so immutable targets can never be replaced in a race."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as error:
        raise SafeFilesystemError("renameat2 is required for immutable atomic writes") from error
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent,
        os.fsencode(_simple_name(source_name)),
        target_parent,
        os.fsencode(_simple_name(target_name)),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise ArtifactExistsError(f"immutable target already exists: {target_name}")
    raise SafeFilesystemError(
        f"atomic no-replace rename failed for {target_name}: {os.strerror(error_number)}"
    )


def _temporary_name(label: str) -> str:
    safe_label = "".join(character if character.isalnum() else "-" for character in label)[:40]
    return f".{safe_label}.tmp-{os.getpid()}-{os.urandom(12).hex()}"


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise SafeFilesystemError("short write to immutable artifact")
        remaining = remaining[written:]


def _write_new_file_at(directory: int, name: str, content: bytes, mode: int) -> None:
    if mode & 0o077:
        raise SafeFilesystemError("private file mode grants group/other access")
    descriptor = os.open(
        _simple_name(name),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW,
        mode,
        dir_fd=directory,
    )
    completed = False
    try:
        _write_all(descriptor, content)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        completed = True
    finally:
        os.close(descriptor)
        if not completed:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory)


def atomic_write_new(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Durably publish one new private file with no symlink traversal or overwrite window."""
    parent_descriptor = open_directory_nofollow(path.parent)
    temporary = _temporary_name(path.name)
    created = False
    try:
        _require_private_directory(parent_descriptor, str(path.parent))
        _simple_name(path.name)
        _write_new_file_at(parent_descriptor, temporary, content, mode)
        created = True
        _rename_noreplace(parent_descriptor, temporary, parent_descriptor, path.name)
        created = False
        os.fsync(parent_descriptor)
    finally:
        if created:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent_descriptor)
        os.close(parent_descriptor)


def publish_flat_directory(
    root: Path,
    name: str,
    files: Mapping[str, bytes],
    *,
    directory_mode: int = 0o500,
    file_mode: int = 0o400,
) -> Path:
    """Durably publish a flat immutable directory through an atomic no-replace rename."""
    if not files:
        raise SafeFilesystemError("immutable directory must contain at least one file")
    if directory_mode & 0o077 or file_mode & 0o077:
        raise SafeFilesystemError("immutable artifact permissions must be private")
    final_name = _simple_name(name)
    validated_files = tuple(
        sorted((_simple_name(path), content) for path, content in files.items())
    )
    root_descriptor = open_directory_nofollow(root)
    temporary = _temporary_name(final_name)
    temporary_descriptor: int | None = None
    temporary_exists = False
    written_names: list[str] = []
    try:
        _require_private_directory(root_descriptor, str(root))
        os.mkdir(temporary, mode=0o700, dir_fd=root_descriptor)
        temporary_exists = True
        temporary_descriptor = os.open(
            temporary,
            _DIRECTORY_FLAGS | _NOFOLLOW,
            dir_fd=root_descriptor,
        )
        for file_name, content in validated_files:
            _write_new_file_at(temporary_descriptor, file_name, content, file_mode)
            written_names.append(file_name)
        os.fchmod(temporary_descriptor, directory_mode)
        os.fsync(temporary_descriptor)
        _rename_noreplace(root_descriptor, temporary, root_descriptor, final_name)
        temporary_exists = False
        os.fsync(root_descriptor)
        return root / final_name
    finally:
        if temporary_exists and temporary_descriptor is not None:
            # The directory has already been sealed read-only before the
            # no-replace rename.  If another publisher wins the race, restore
            # owner write permission solely on our unlinked temporary tree so
            # cleanup works for non-root service/CI users as well.
            os.fchmod(temporary_descriptor, 0o700)
            for file_name in written_names:
                with suppress(FileNotFoundError):
                    os.unlink(file_name, dir_fd=temporary_descriptor)
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_exists:
            with suppress(FileNotFoundError):
                os.rmdir(temporary, dir_fd=root_descriptor)
        os.close(root_descriptor)


def _validated_tree_files(files: Mapping[str, bytes]) -> tuple[tuple[tuple[str, ...], bytes], ...]:
    if not files:
        raise SafeFilesystemError("immutable directory must contain at least one file")
    validated: list[tuple[tuple[str, ...], bytes]] = []
    file_paths: set[tuple[str, ...]] = set()
    for relative, content in files.items():
        if not isinstance(relative, str) or not isinstance(content, bytes):
            raise SafeFilesystemError("immutable tree entries must be relative strings and bytes")
        parts = relative_parts(relative)
        if parts in file_paths:
            raise SafeFilesystemError(f"duplicate immutable tree path: {relative}")
        file_paths.add(parts)
        validated.append((parts, content))
    for parts in file_paths:
        for index in range(1, len(parts)):
            if parts[:index] in file_paths:
                raise SafeFilesystemError(
                    f"immutable tree file is also a directory prefix: {'/'.join(parts[:index])}"
                )
    return tuple(sorted(validated, key=lambda item: item[0]))


def _open_or_create_private_child(parent: int, name: str) -> int:
    with suppress(FileExistsError):
        os.mkdir(_simple_name(name), mode=0o700, dir_fd=parent)
    child = os.open(_simple_name(name), _DIRECTORY_FLAGS | _NOFOLLOW, dir_fd=parent)
    _require_private_directory(child, name)
    return child


def _remove_private_tree(descriptor: int) -> None:
    """Remove only an unpublished temporary tree created by this module."""
    os.fchmod(descriptor, 0o700)
    for name in sorted(os.listdir(descriptor), reverse=True):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, _DIRECTORY_FLAGS | _NOFOLLOW, dir_fd=descriptor)
            try:
                _remove_private_tree(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        elif stat.S_ISREG(metadata.st_mode):
            os.unlink(name, dir_fd=descriptor)
        else:
            raise SafeFilesystemError(f"special file appeared in temporary tree: {name}")


def publish_tree_directory(
    root: Path,
    name: str,
    files: Mapping[str, bytes],
    *,
    directory_mode: int = 0o500,
    file_mode: int = 0o400,
) -> Path:
    """Atomically publish a nested immutable directory without following symlinks."""
    if directory_mode != 0o500 or file_mode != 0o400:
        raise SafeFilesystemError("immutable package modes must be exactly 0500/0400")
    final_name = _simple_name(name)
    validated_files = _validated_tree_files(files)
    root_descriptor = open_directory_nofollow(root)
    temporary = _temporary_name(final_name)
    temporary_descriptor: int | None = None
    temporary_exists = False
    try:
        _require_private_directory(root_descriptor, str(root))
        os.mkdir(temporary, mode=0o700, dir_fd=root_descriptor)
        temporary_exists = True
        temporary_descriptor = os.open(
            temporary,
            _DIRECTORY_FLAGS | _NOFOLLOW,
            dir_fd=root_descriptor,
        )
        for parts, content in validated_files:
            descriptor = temporary_descriptor
            opened: list[int] = []
            try:
                for component in parts[:-1]:
                    child = _open_or_create_private_child(descriptor, component)
                    opened.append(child)
                    descriptor = child
                _write_new_file_at(descriptor, parts[-1], content, 0o600)
            finally:
                for opened_descriptor in reversed(opened):
                    os.close(opened_descriptor)
        _seal_tree_descriptor(temporary_descriptor)
        os.fchmod(temporary_descriptor, directory_mode)
        os.fsync(temporary_descriptor)
        _rename_noreplace(root_descriptor, temporary, root_descriptor, final_name)
        temporary_exists = False
        os.fsync(root_descriptor)
        return root / final_name
    finally:
        if temporary_exists and temporary_descriptor is not None:
            _remove_private_tree(temporary_descriptor)
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_exists:
            with suppress(FileNotFoundError):
                os.rmdir(temporary, dir_fd=root_descriptor)
        os.close(root_descriptor)


def _stable_regular_file(
    descriptor: int,
    label: str,
    *,
    expected_mode: int | None = None,
) -> tuple[bytes, os.stat_result]:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise SafeFilesystemError(f"not a regular file: {label}")
    if before.st_nlink != 1:
        raise SafeFilesystemError(f"immutable file must have exactly one link: {label}")
    actual_mode = stat.S_IMODE(before.st_mode)
    if expected_mode is not None and actual_mode != expected_mode:
        raise SafeFilesystemError(f"file mode differs from required {expected_mode:#05o}: {label}")
    if expected_mode is None and actual_mode & 0o077:
        raise SafeFilesystemError(f"file permissions are broader than private: {label}")
    chunks: list[bytes] = []
    total = 0
    while chunk := os.read(descriptor, _READ_CHUNK):
        total += len(chunk)
        if total > 64 * 1024 * 1024:
            raise SafeFilesystemError(f"immutable file exceeds 64 MiB: {label}")
        chunks.append(chunk)
    after = os.fstat(descriptor)
    signature_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    signature_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if signature_before != signature_after:
        raise SafeFilesystemError(f"file changed while it was being consumed: {label}")
    return b"".join(chunks), after


def read_regular_file_at(
    directory_descriptor: int,
    name: str,
    *,
    label: str,
    expected_mode: int | None = None,
) -> bytes:
    """Read a stable regular file relative to an already pinned directory."""
    descriptor = os.open(
        _simple_name(name),
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW,
        dir_fd=directory_descriptor,
    )
    try:
        content, _metadata = _stable_regular_file(
            descriptor,
            label,
            expected_mode=expected_mode,
        )
        return content
    finally:
        os.close(descriptor)


def read_regular_file(path: Path, *, expected_mode: int | None = None) -> bytes:
    """Read a private regular file, optionally requiring one exact immutable mode."""
    parent_descriptor = open_directory_nofollow(path.parent)
    try:
        return read_regular_file_at(
            parent_descriptor,
            path.name,
            label=str(path),
            expected_mode=expected_mode,
        )
    finally:
        os.close(parent_descriptor)


def open_regular_file_nofollow(path: Path, *, expected_mode: int | None = None) -> int:
    """Pin a private single-link regular file without following any path component."""
    parent_descriptor: int | None = None
    descriptor: int | None = None
    try:
        parent_descriptor = open_directory_nofollow(path.parent)
        descriptor = os.open(
            _simple_name(path.name),
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SafeFilesystemError(f"file must be regular and single-link: {path}")
        mode = stat.S_IMODE(metadata.st_mode)
        if expected_mode is not None and mode != expected_mode:
            raise SafeFilesystemError(
                f"file mode differs from required {expected_mode:#05o}: {path}"
            )
        if expected_mode is None and mode & 0o077:
            raise SafeFilesystemError(f"file permissions are broader than private: {path}")
        result = descriptor
        descriptor = None
        return result
    except OSError as error:
        raise SafeFilesystemError(
            f"cannot pin regular file without symlinks: {path}: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _read_tree_into(files: dict[str, bytes], descriptor: int, prefix: PurePosixPath) -> None:
    before = os.fstat(descriptor)
    if not stat.S_ISDIR(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o500:
        raise SafeFilesystemError(f"immutable directory mode must be exactly 0500: {prefix}")
    initial_names = tuple(sorted(os.listdir(descriptor)))
    for name in initial_names:
        _simple_name(name)
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        relative = prefix / name
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, _DIRECTORY_FLAGS | _NOFOLLOW, dir_fd=descriptor)
            try:
                _read_tree_into(files, child, relative)
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            files[relative.as_posix()] = read_regular_file_at(
                descriptor,
                name,
                label=relative.as_posix(),
                expected_mode=0o400,
            )
        else:
            raise SafeFilesystemError(f"special file in immutable tree: {relative}")
    final_names = tuple(sorted(os.listdir(descriptor)))
    after = os.fstat(descriptor)
    signature_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    signature_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if initial_names != final_names or signature_before != signature_after:
        raise SafeFilesystemError(f"immutable directory changed while read: {prefix}")


def read_tree_files(root: Path) -> dict[str, bytes]:
    """Read a complete exact-mode immutable tree while every directory stays pinned."""
    descriptor = open_directory_nofollow(root)
    try:
        files: dict[str, bytes] = {}
        _read_tree_into(files, descriptor, PurePosixPath())
        if not files:
            raise SafeFilesystemError("immutable tree contains no files")
        return files
    finally:
        os.close(descriptor)


def write_new_relative(
    root: Path,
    relative: str | PurePosixPath,
    content: bytes,
    mode: int = 0o600,
) -> Path:
    """Write below a pinned capability root, rejecting traversal and symlink components."""
    parts = relative_parts(relative)
    root_descriptor = open_directory_nofollow(root)
    descriptor = root_descriptor
    try:
        _require_private_directory(root_descriptor, str(root))
        for component in parts[:-1]:
            next_descriptor = os.open(
                component,
                _DIRECTORY_FLAGS | _NOFOLLOW,
                dir_fd=descriptor,
            )
            if descriptor != root_descriptor:
                os.close(descriptor)
            descriptor = next_descriptor
        temporary = _temporary_name(parts[-1])
        created = False
        try:
            _require_private_directory(descriptor, f"{root}/{PurePosixPath(*parts[:-1])}")
            _write_new_file_at(descriptor, temporary, content, mode)
            created = True
            _rename_noreplace(descriptor, temporary, descriptor, parts[-1])
            created = False
            os.fsync(descriptor)
        finally:
            if created:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=descriptor)
        return root.joinpath(*parts)
    finally:
        if descriptor != root_descriptor:
            os.close(descriptor)
        os.close(root_descriptor)


def _tree_digest_into(digest: hashlib._Hash, descriptor: int, prefix: PurePosixPath) -> None:
    for name in sorted(os.listdir(descriptor)):
        _simple_name(name)
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        relative = (prefix / name).as_posix().encode("utf-8")
        if stat.S_ISLNK(metadata.st_mode):
            raise SafeFilesystemError(f"symlink in private tree: {relative.decode()}")
        if stat.S_ISDIR(metadata.st_mode):
            digest.update(b"D\0" + relative + b"\0")
            digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(2, "big"))
            child = os.open(name, _DIRECTORY_FLAGS | _NOFOLLOW, dir_fd=descriptor)
            try:
                _tree_digest_into(digest, child, prefix / name)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SafeFilesystemError(f"special file in private tree: {relative.decode()}")
        file_descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW,
            dir_fd=descriptor,
        )
        try:
            content, _stable_metadata = _stable_regular_file(
                file_descriptor, relative.decode("utf-8")
            )
        finally:
            os.close(file_descriptor)
        digest.update(b"F\0" + relative + b"\0")
        digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(2, "big"))
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)


def private_tree_sha256(root: Path) -> str:
    """Hash the names, types and exact file bytes of a no-symlink private tree."""
    descriptor = open_directory_nofollow(root)
    try:
        digest = hashlib.sha256(b"radar-v2-private-tree/v1\0")
        _tree_digest_into(digest, descriptor, PurePosixPath())
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _seal_tree_descriptor(descriptor: int) -> None:
    for name in sorted(os.listdir(descriptor)):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise SafeFilesystemError(f"refusing to seal a tree containing symlink: {name}")
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, _DIRECTORY_FLAGS | _NOFOLLOW, dir_fd=descriptor)
            try:
                _seal_tree_descriptor(child)
                os.fchmod(child, 0o500)
                os.fsync(child)
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            file_descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW,
                dir_fd=descriptor,
            )
            try:
                os.fchmod(file_descriptor, 0o400)
                os.fsync(file_descriptor)
            finally:
                os.close(file_descriptor)
        else:
            raise SafeFilesystemError(f"refusing to seal special file: {name}")


def seal_private_tree(root: Path) -> None:
    """Remove write permissions recursively after a branch has completed."""
    descriptor = open_directory_nofollow(root)
    try:
        _seal_tree_descriptor(descriptor)
        os.fchmod(descriptor, 0o500)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ArtifactExistsError",
    "PathEscapeError",
    "SafeFilesystemError",
    "atomic_write_new",
    "create_private_directory",
    "ensure_private_directory",
    "open_directory_nofollow",
    "open_regular_file_nofollow",
    "private_tree_sha256",
    "publish_flat_directory",
    "publish_tree_directory",
    "read_regular_file",
    "read_regular_file_at",
    "read_tree_files",
    "relative_parts",
    "seal_private_tree",
    "write_new_relative",
]
