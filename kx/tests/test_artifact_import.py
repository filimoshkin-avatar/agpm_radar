"""The offline import path: manifests, provenance, and the guards around both."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from conftest import connect, one
from radar_kx.artifact_import import (
    ArtifactManifestError,
    import_artifact,
    load_artifact_manifest,
    load_provenance_corrections,
    record_provenance_corrections,
)
from radar_kx.config import Settings
from radar_kx.database import NETWORK_SOURCE_KINDS, Database, VersionProvenance
from radar_kx.fetcher import DocumentTask, FetchResult
from radar_kx.url_policy import UnsafeUrlError, canon_url, canonical_identity_url

BACKFILL = Path(__file__).resolve().parents[1] / "data" / "provenance-backfill-2026-08-22.json"

PAGE = (
    "<html><head><title>Enterprise AI agents</title></head><body><article>"
    + "<p>We tested six enterprise AI agents on real workflows and wrote down what broke.</p>" * 12
    + "</article></body></html>"
)


def _settings(dsn: str) -> Settings:
    return Settings(
        dsn=dsn,
        release_id="test",
        # Any readable directory: the setting only feeds a free-space check.
        capacity_path=str(Path(__file__).resolve().parent),
        user_agent="test",
        request_timeout_seconds=30.0,
        connect_timeout_seconds=10.0,
        per_host_interval_seconds=1.0,
        max_body_bytes=15 * 1024 * 1024,
        min_text_chars=200,
        min_free_bytes=1024,
        lease_seconds=300,
        max_attempts=4,
        max_in_flight_per_host=8,
        respect_robots=True,
    )


def _manifest(tmp_path: Path, documents: list[dict[str, Any]], **artifact: Any) -> Path:
    payload: dict[str, Any] = {
        "artifact": {
            "name": "test-artifact",
            "recorded_by": "test",
            "source_kind": "operator_artifact",
            "provided_by": "ivan",
            "provided_at": "2026-08-22T06:00:00Z",
            **artifact,
        },
        "documents": documents,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _page(tmp_path: Path, name: str = "page.html", body: str = PAGE) -> str:
    (tmp_path / name).write_text(body, encoding="utf-8")
    return name


def test_a_file_with_no_recorded_origin_is_refused(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [{"canonical_url": "https://example.com/a", "path": _page(tmp_path)}],
    )
    with pytest.raises(ArtifactManifestError, match="no provenance"):
        load_artifact_manifest(manifest)


def test_a_file_may_not_claim_a_request_nobody_made(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [
            {
                "canonical_url": "https://example.com/a",
                "path": _page(tmp_path),
                "provenance": {"source_access_method": "http_default"},
            }
        ],
    )
    with pytest.raises(ArtifactManifestError, match="source_access_method"):
        load_artifact_manifest(manifest)


def test_an_archive_import_without_snapshot_identity_is_refused(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [
            {
                "canonical_url": "https://adopt.ai/blog/enterprise-ai-agents",
                "path": _page(tmp_path),
                "provenance": {"source_access_method": "web_archive"},
            }
        ],
    )
    with pytest.raises(ArtifactManifestError, match="archive_url and archive_captured_at"):
        load_artifact_manifest(manifest)


def test_an_archive_import_may_declare_itself_unfinished(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [
            {
                "canonical_url": "https://adopt.ai/blog/enterprise-ai-agents",
                "path": _page(tmp_path),
                "provenance": {
                    "source_access_method": "web_archive",
                    "manual_review_required": True,
                    "manual_review_reason": "snapshot URL was not recorded",
                },
            }
        ],
    )
    loaded = load_artifact_manifest(manifest)
    assert loaded.documents[0].provenance.archive_used is True
    assert loaded.documents[0].provenance.manual_review_required is True


def test_a_manifest_may_not_reach_outside_its_own_directory(tmp_path: Path) -> None:
    (tmp_path.parent / "secret.html").write_text(PAGE, encoding="utf-8")
    manifest = _manifest(
        tmp_path,
        [
            {
                "canonical_url": "https://example.com/a",
                "path": "../secret.html",
                "provenance": {"source_access_method": "operator_file"},
            }
        ],
    )
    with pytest.raises(ArtifactManifestError, match="escapes the manifest directory"):
        load_artifact_manifest(manifest)


def test_one_url_may_not_appear_twice(tmp_path: Path) -> None:
    entry = {
        "canonical_url": "https://example.com/a",
        "path": _page(tmp_path),
        "provenance": {"source_access_method": "operator_file"},
    }
    with pytest.raises(ArtifactManifestError, match="duplicate canonical URL"):
        load_artifact_manifest(_manifest(tmp_path, [entry, dict(entry)]))


def test_canon_documents_get_a_reserved_identity(tmp_path: Path) -> None:
    assert canon_url("white-paper-v1.2.md") == "agpm-canon:/white-paper-v1.2.md"
    assert canonical_identity_url("agpm-canon:/raw/manifesto-v3.md") == (
        "agpm-canon:/raw/manifesto-v3.md"
    )
    # The reserved scheme cannot be used to walk out of the canon directory.
    with pytest.raises(UnsafeUrlError):
        canon_url("../../etc/passwd")
    with pytest.raises(UnsafeUrlError):
        canon_url("  ")
    # http(s) still goes through the ordinary normalizer, so identity stays shared
    # with every other store.
    assert canonical_identity_url("HTTPS://Example.com/a?utm_source=x") == "https://example.com/a"


class _OfflineTask:
    """A task that claims an offline source kind. The fetch path must refuse it."""

    document_id = "a" * 64
    canonical_url = "https://example.com/a"
    attempt_count = 1
    source_kind = "operator_artifact"


def test_a_fetch_may_never_be_recorded_as_an_operator_artifact() -> None:
    # Defect D9 in one assertion: two ordinary browser-header fetches entered the
    # evidence base labelled as material an operator had handed over, and
    # fetch_attempts rows are immutable, so the mislabelling could not be undone.
    # The guard fires before any connection is attempted, which is why a DSN that
    # points at nothing is enough to prove it.
    database = Database(_settings("dbname=nonexistent-on-purpose"))
    result = FetchResult(
        task=_OfflineTask(),  # type: ignore[arg-type]
        response=None,
        parsed=None,
        error_code="http_403",
        error_detail=None,
        retryable=False,
        not_modified=False,
    )
    with pytest.raises(ValueError, match="a fetch may not record source kind"):
        database.record_fetch_result(result)


def test_the_ordinary_fetch_kinds_are_still_allowed() -> None:
    for kind in ("network", "network_robots_override"):
        assert kind in NETWORK_SOURCE_KINDS
    assert (
        DocumentTask(
            document_id="a" * 64,
            canonical_url="https://example.com/a",
            attempt_count=1,
            etag=None,
            last_modified=None,
            robots_override=True,
        ).source_kind
        in NETWORK_SOURCE_KINDS
    )


def test_the_shipped_backfill_file_is_loadable_and_complete() -> None:
    recorded_by, corrections = load_provenance_corrections(BACKFILL)
    assert recorded_by
    assert len(corrections) == 25
    methods = [provenance.source_access_method for _, provenance in corrections]
    # 23 documents came from the owner's HTML artifact, four of those actually from
    # a web archive; two were ordinary browser-header fetches mislabelled by the
    # hotfix (defect D9).
    assert methods.count("operator_file") == 19
    assert methods.count("web_archive") == 4
    assert methods.count("browser_headers") == 2
    blocked = [url for url, provenance in corrections if provenance.manual_review_required]
    assert len(blocked) == 4
    assert "https://adopt.ai/blog/enterprise-ai-agents" in blocked


@pytest.fixture
def database(migrated_dsn: str) -> Database:
    return Database(_settings(migrated_dsn))


def test_import_stores_the_version_and_its_provenance(
    database: Database, migrated_dsn: str, tmp_path: Path
) -> None:
    manifest = load_artifact_manifest(
        _manifest(
            tmp_path,
            [
                {
                    "canonical_url": "https://adopt.ai/blog/enterprise-ai-agents",
                    "path": _page(tmp_path),
                    "content_type": "text/html; charset=utf-8",
                    "fetched_at": "2026-08-22T06:19:43Z",
                    "provenance": {
                        "source_access_method": "web_archive",
                        "manual_review_required": True,
                        "manual_review_reason": "snapshot URL was not recorded",
                    },
                }
            ],
        )
    )
    result = import_artifact(database, manifest)
    assert result.versions_created == 1
    assert result.complete_versions == 1
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT source_kind FROM kx.document_versions")
        assert one(cursor)["source_kind"] == "operator_artifact"
        cursor.execute("SELECT count(*) AS count FROM kx.fetch_attempts")
        # No HTTP request happened, so none is recorded. Inventing one is the D9 bug.
        assert one(cursor)["count"] == 0
        cursor.execute("SELECT block_reason FROM kx.version_publication_block")
        assert one(cursor)["block_reason"] == "provenance_manual_review"


def test_reimporting_the_same_artifact_changes_nothing(
    database: Database, migrated_dsn: str, tmp_path: Path
) -> None:
    manifest = load_artifact_manifest(
        _manifest(
            tmp_path,
            [
                {
                    "canonical_url": "https://example.com/a",
                    "path": _page(tmp_path),
                    "provenance": {"source_access_method": "operator_file"},
                }
            ],
        )
    )
    first = import_artifact(database, manifest)
    second = import_artifact(database, manifest)
    assert (first.versions_created, second.versions_created) == (1, 0)
    assert second.versions_already_present == 1
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS count FROM kx.document_versions")
        assert one(cursor)["count"] == 1
        # Append-only does not mean append-duplicates: the second run says nothing new.
        cursor.execute("SELECT count(*) AS count FROM kx.version_provenance")
        assert one(cursor)["count"] == 1


def test_metadata_does_not_become_full_text(
    database: Database, migrated_dsn: str, tmp_path: Path
) -> None:
    thin = "<html><head><title>Paywall</title></head><body><p>Sign in to read.</p></body></html>"
    manifest = load_artifact_manifest(
        _manifest(
            tmp_path,
            [
                {
                    "canonical_url": "https://example.com/paywalled",
                    "path": _page(tmp_path, "thin.html", thin),
                    "provenance": {"source_access_method": "operator_file"},
                }
            ],
        )
    )
    result = import_artifact(database, manifest)
    assert result.complete_versions == 0
    assert result.incomplete_versions == ("https://example.com/paywalled",)
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT is_complete FROM kx.document_versions")
        assert one(cursor)["is_complete"] is False
        # An incomplete version never becomes the document's best version, and the
        # queue is not told the gap is closed.
        cursor.execute("SELECT best_version_id FROM kx.documents")
        assert one(cursor)["best_version_id"] is None


def test_a_file_that_parses_to_nothing_is_refused(database: Database, tmp_path: Path) -> None:
    manifest = load_artifact_manifest(
        _manifest(
            tmp_path,
            [
                {
                    "canonical_url": "https://example.com/empty",
                    "path": _page(tmp_path, "empty.html", "<html><body></body></html>"),
                    "provenance": {"source_access_method": "operator_file"},
                }
            ],
        )
    )
    with pytest.raises(ArtifactManifestError, match="metadata is not full text"):
        import_artifact(database, manifest)


def test_provenance_corrections_append_once_and_then_stay_quiet(
    database: Database, migrated_dsn: str, tmp_path: Path
) -> None:
    manifest = load_artifact_manifest(
        _manifest(
            tmp_path,
            [
                {
                    "canonical_url": "https://appian.com/blog/pm/building-enterprise-grade-ai-agents",
                    "path": _page(tmp_path),
                    "provenance": {"source_access_method": "operator_file"},
                }
            ],
        )
    )
    import_artifact(database, manifest)
    correction = VersionProvenance(
        source_access_method="browser_headers",
        notes="defect D9: fetch_attempts holds a successful browser-header HTTP 200",
    )
    first = record_provenance_corrections(
        database,
        recorded_by="test",
        corrections=[
            ("https://appian.com/blog/pm/building-enterprise-grade-ai-agents", correction)
        ],
    )
    second = record_provenance_corrections(
        database,
        recorded_by="test",
        corrections=[
            ("https://appian.com/blog/pm/building-enterprise-grade-ai-agents", correction)
        ],
    )
    assert (first["appended"], first["unchanged"]) == (1, 0)
    assert (second["appended"], second["unchanged"]) == (0, 1)
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT source_access_method FROM kx.version_provenance_current")
        assert one(cursor)["source_access_method"] == "browser_headers"
        # The version keeps the source kind it was written with: it is immutable, and
        # the correction is what says otherwise.
        cursor.execute("SELECT source_kind FROM kx.document_versions")
        assert one(cursor)["source_kind"] == "operator_artifact"


def test_a_correction_for_a_document_we_do_not_hold_is_reported_not_swallowed(
    database: Database,
) -> None:
    outcome = record_provenance_corrections(
        database,
        recorded_by="test",
        corrections=[
            (
                "https://example.com/never-seen",
                VersionProvenance(source_access_method="http_default"),
            )
        ],
    )
    assert outcome["documentsNotInStore"] == ["https://example.com/never-seen"]
    assert outcome["appended"] == 0


def test_canon_documents_import_under_the_reserved_scheme(
    database: Database, migrated_dsn: str, tmp_path: Path
) -> None:
    body = "# White paper\n\n" + ("Agentic project management is a governance model. " * 40)
    manifest = load_artifact_manifest(
        _manifest(
            tmp_path,
            [
                {
                    "canonical_url": "agpm-canon:/raw/agpm-white-paper-v1.2.md",
                    "path": _page(tmp_path, "white-paper.md", body),
                    "content_type": "text/plain; charset=utf-8",
                    "source_kind": "local_import",
                    "provenance": {
                        "source_access_method": "local_import",
                        "provided_by": "project-manager",
                        "provided_at": "2026-08-22T00:00:00Z",
                    },
                }
            ],
        )
    )
    result = import_artifact(database, manifest)
    assert result.complete_versions == 1
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT canonical_url FROM kx.documents")
        assert one(cursor)["canonical_url"] == "agpm-canon:/raw/agpm-white-paper-v1.2.md"
        cursor.execute("SELECT source_kind FROM kx.document_versions")
        assert one(cursor)["source_kind"] == "local_import"


def test_datetimes_in_the_backfill_are_timezone_aware() -> None:
    _, corrections = load_provenance_corrections(BACKFILL)
    for _, provenance in corrections:
        if provenance.provided_at is not None:
            assert provenance.provided_at.tzinfo is not None
            assert provenance.provided_at <= datetime(2026, 12, 31, tzinfo=UTC)
