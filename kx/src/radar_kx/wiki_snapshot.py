"""An immutable snapshot of the file wiki, with a checksum (P27, slice 2.5a).

ADR-0006 gives every score and every published statement a `knowledge_release_id`.
That identifier is worthless if nobody can say what the wiki looked like when the
release was cut: `knowledge/` is markdown on a control host, is not under version
control, and is edited by hand between releases. Without a snapshot, "this rating
was computed against the wiki" names nothing.

The snapshot is content-addressed. Its id is derived from the manifest hash, so
taking one twice with nothing changed returns the same id and stores nothing
twice, and `taken_at` records when that content was first seen. A knowledge
release points at an id; the id points at exactly one set of bytes.

Storage is content-addressed too. `wiki_blobs` is keyed by the file's own hash, so
the 30 MB PDF in `raw/originals` is stored once no matter how many snapshots
contain it, and a snapshot after an ordinary week of editing costs a few kilobytes.

The bundle is built on the control host, where the files are, and recorded on the
host that holds KX - the same split as the store reconciliation of slice 2.4a.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from radar_kx.identifiers import sha256_bytes

#: Prefix of a snapshot id, so the perimeter is legible in a foreign key.
SNAPSHOT_PREFIX = "wiki"

#: How much of the manifest hash goes into the id. Sixteen hex characters is 64
#: bits: enough that a collision is not a thing to plan for, short enough to read.
ID_HASH_CHARS = 16

#: A single file larger than this is refused rather than silently truncated or
#: silently accepted: it is a sign that something other than a wiki is being
#: snapshotted, and that is worth stopping for.
MAX_FILE_BYTES = 256 * 1024 * 1024


class WikiSnapshotError(ValueError):
    """The bundle cannot be read or does not describe a wiki."""


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    relative_path: str
    blob_sha256: str
    bytes_: int
    content: bytes


@dataclass(frozen=True, slots=True)
class WikiSnapshot:
    perimeter: str
    files: tuple[SnapshotFile, ...]

    @property
    def manifest_sha256(self) -> str:
        """One value over the sorted "path sha256" lines.

        Sorted, so the order files happen to be walked in cannot change the
        identity of a snapshot; and over paths as well as contents, so moving a
        file is a change even when no byte of it differs.
        """
        lines = "".join(
            f"{item.relative_path}\t{item.blob_sha256}\n"
            for item in sorted(self.files, key=lambda item: item.relative_path)
        )
        return sha256_bytes(lines.encode("utf-8"))

    @property
    def snapshot_id(self) -> str:
        return f"{SNAPSHOT_PREFIX}-{self.perimeter}-{self.manifest_sha256[:ID_HASH_CHARS]}"

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes_ for item in self.files)


def _members(archive: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    for member in archive.getmembers():
        if member.isdir():
            continue
        if not member.isfile():
            # A symlink or a device in a wiki bundle is not a wiki file, and
            # following one would let the bundle name anything on the host.
            raise WikiSnapshotError(f"{member.name} is not a regular file")
        if member.name.startswith("/") or ".." in Path(member.name).parts:
            raise WikiSnapshotError(f"{member.name} escapes the bundle")
        if member.size > MAX_FILE_BYTES:
            raise WikiSnapshotError(f"{member.name} is {member.size} bytes")
        yield member


def read_bundle(path: Path, *, perimeter: str) -> WikiSnapshot:
    """Read a tar.gz of the wiki tree into a snapshot."""
    files: list[SnapshotFile] = []
    seen: set[str] = set()
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in _members(archive):
                handle = archive.extractfile(member)
                if handle is None:
                    raise WikiSnapshotError(f"{member.name} has no content")
                content = handle.read()
                relative = member.name.removeprefix("./")
                if relative in seen:
                    raise WikiSnapshotError(f"{relative} appears twice in the bundle")
                seen.add(relative)
                files.append(
                    SnapshotFile(
                        relative_path=relative,
                        blob_sha256=hashlib.sha256(content).hexdigest(),
                        bytes_=len(content),
                        content=content,
                    )
                )
    except (OSError, tarfile.TarError) as exc:
        raise WikiSnapshotError(f"bundle is unreadable: {exc}") from exc
    if not files:
        raise WikiSnapshotError("bundle holds no files")
    return WikiSnapshot(perimeter=perimeter, files=tuple(files))


def compress(content: bytes) -> bytes:
    """Deterministic gzip: no timestamp, so identical bytes give identical blobs."""
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as handle:
        handle.write(content)
    return buffer.getvalue()
