"""Slice 2.5a: what a knowledge_release_id actually points at."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import connect
from radar_kx.config import Settings
from radar_kx.database import Database
from radar_kx.wiki_snapshot import (
    WikiSnapshotError,
    compress,
    read_bundle,
)


def _settings(dsn: str) -> Settings:
    base = Settings.from_environment()
    return Settings(
        **{
            **{field: getattr(base, field) for field in Settings.__dataclass_fields__},
            "dsn": dsn,
            "min_free_bytes": 1024,
            "capacity_path": str(Path(__file__).resolve().parent),
        }
    )


def _bundle(path: Path, files: dict[str, bytes], *, mtime: int = 0) -> Path:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mtime = mtime
            archive.addfile(info, io.BytesIO(content))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0) as handle:
        handle.write(buffer.getvalue())
    path.write_bytes(packed.getvalue())
    return path


WIKI = {
    "overview/agpm-overview.md": b"# AgPM\n\nThe compiled model.\n",
    "SCHEMA.md": b"# Conventions\n\n## Purpose\n",
}


def test_the_same_files_are_the_same_snapshot_however_they_were_packed(tmp_path: Path) -> None:
    first = read_bundle(_bundle(tmp_path / "a.tar.gz", WIKI, mtime=0), perimeter="agpm")
    second = read_bundle(_bundle(tmp_path / "b.tar.gz", WIKI, mtime=999), perimeter="agpm")
    assert first.snapshot_id == second.snapshot_id
    assert first.manifest_sha256 == second.manifest_sha256


def test_moving_a_file_changes_the_snapshot_even_when_no_byte_of_it_does(
    tmp_path: Path,
) -> None:
    # The manifest hashes paths as well as contents, because a page that moved is
    # a wiki that changed.
    original = read_bundle(_bundle(tmp_path / "a.tar.gz", WIKI), perimeter="agpm")
    moved = dict(WIKI)
    moved["model/agpm-overview.md"] = moved.pop("overview/agpm-overview.md")
    after = read_bundle(_bundle(tmp_path / "b.tar.gz", moved), perimeter="agpm")
    assert original.manifest_sha256 != after.manifest_sha256


def test_a_bundle_that_escapes_itself_is_refused(tmp_path: Path) -> None:
    for name in ("../outside.md", "/etc/passwd"):
        path = _bundle(tmp_path / "bad.tar.gz", {name: b"x"})
        with pytest.raises(WikiSnapshotError, match="escapes the bundle"):
            read_bundle(path, perimeter="agpm")


def test_an_empty_bundle_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WikiSnapshotError, match="no files"):
        read_bundle(_bundle(tmp_path / "empty.tar.gz", {}), perimeter="agpm")


def test_compression_is_deterministic() -> None:
    # A gzip header carries an mtime by default, which would give identical bytes
    # two different blob hashes and defeat the content addressing.
    assert compress(b"the same bytes") == compress(b"the same bytes")


def test_a_snapshot_is_stored_once_and_recognised_the_second_time(
    migrated_dsn: str, tmp_path: Path
) -> None:
    database = Database(_settings(migrated_dsn))
    snapshot = read_bundle(_bundle(tmp_path / "wiki.tar.gz", WIKI), perimeter="agpm")
    first = database.record_wiki_snapshot(snapshot, recorded_by="test")
    assert first["alreadyStored"] is False
    assert first["fileCount"] == 2
    assert first["newBlobs"] == 2

    second = database.record_wiki_snapshot(snapshot, recorded_by="test")
    assert second["alreadyStored"] is True
    assert second["snapshotId"] == first["snapshotId"]

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS total FROM kx.wiki_snapshots")
        assert cursor.fetchone()["total"] == 1  # type: ignore[index]


def test_an_unchanged_file_is_not_stored_twice_across_snapshots(
    migrated_dsn: str, tmp_path: Path
) -> None:
    # This is what makes a snapshot per release affordable: after the first one,
    # a week of ordinary editing costs the pages that changed.
    database = Database(_settings(migrated_dsn))
    database.record_wiki_snapshot(
        read_bundle(_bundle(tmp_path / "one.tar.gz", WIKI), perimeter="agpm"),
        recorded_by="test",
    )
    edited = dict(WIKI)
    edited["SCHEMA.md"] = b"# Conventions\n\n## Purpose\n\n## Supporting sources\n"
    outcome = database.record_wiki_snapshot(
        read_bundle(_bundle(tmp_path / "two.tar.gz", edited), perimeter="agpm"),
        recorded_by="test",
    )
    assert outcome["fileCount"] == 2
    assert outcome["newBlobs"] == 1  # only the page that changed

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS total FROM kx.wiki_blobs")
        assert cursor.fetchone()["total"] == 3  # type: ignore[index]
        cursor.execute("SELECT count(*) AS total FROM kx.wiki_snapshot_files")
        assert cursor.fetchone()["total"] == 4  # type: ignore[index]


def test_the_stored_blob_gives_the_file_back(migrated_dsn: str, tmp_path: Path) -> None:
    database = Database(_settings(migrated_dsn))
    snapshot = read_bundle(_bundle(tmp_path / "wiki.tar.gz", WIKI), perimeter="agpm")
    database.record_wiki_snapshot(snapshot, recorded_by="test")
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT blobs.content FROM kx.wiki_snapshot_files AS files"
            " JOIN kx.wiki_blobs AS blobs USING (blob_sha256)"
            " WHERE files.relative_path = 'SCHEMA.md'"
        )
        row = cursor.fetchone()
        assert row is not None
        assert gzip.decompress(cast(bytes, row["content"])) == WIKI["SCHEMA.md"]


def test_a_snapshot_cannot_be_rewritten(migrated_dsn: str, tmp_path: Path) -> None:
    database = Database(_settings(migrated_dsn))
    database.record_wiki_snapshot(
        read_bundle(_bundle(tmp_path / "wiki.tar.gz", WIKI), perimeter="agpm"),
        recorded_by="test",
    )
    for statement in (
        "UPDATE kx.wiki_snapshots SET perimeter = 'edited'",
        "UPDATE kx.wiki_blobs SET raw_bytes = 0",
        "DELETE FROM kx.wiki_snapshot_files",
    ):
        with (
            connect(migrated_dsn) as connection,
            connection.cursor() as cursor,
            pytest.raises(Exception, match="immutable|reject"),
        ):
            cursor.execute(statement)


def test_listing_says_what_is_stored(migrated_dsn: str, tmp_path: Path) -> None:
    database = Database(_settings(migrated_dsn))
    database.record_wiki_snapshot(
        read_bundle(_bundle(tmp_path / "wiki.tar.gz", WIKI), perimeter="agpm"),
        recorded_by="test",
        notes="before the first release",
    )
    listed: list[dict[str, Any]] = database.wiki_snapshots()
    assert len(listed) == 1
    assert listed[0]["perimeter"] == "agpm"
    assert listed[0]["notes"] == "before the first release"
