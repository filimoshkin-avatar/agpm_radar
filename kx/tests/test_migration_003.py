"""Migration 003 against a real PostgreSQL, on both baselines that exist today."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from conftest import MIGRATION_004, _apply, apply_migration_003, connect, one

NOW = datetime(2026, 8, 22, 6, 19, 43, tzinfo=UTC)


def _scalar(dsn: str, query: str, parameters: tuple[Any, ...] = ()) -> Any:
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(query, parameters)
        row = cursor.fetchone()
    return None if row is None else next(iter(row.values()))


def _execute(dsn: str, query: str, parameters: tuple[Any, ...] = ()) -> None:
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(query, parameters)


def _seed_version(dsn: str, *, source_kind: str = "network", url: str | None = None) -> str:
    """Insert the minimum chain documents -> raw_blobs -> document_versions."""
    canonical_url = url or "https://example.com/a"
    document = hashlib.sha256(canonical_url.encode()).hexdigest()
    raw = hashlib.sha256(b"body").hexdigest()
    text_hash = hashlib.sha256(b"text").hexdigest()
    version = hashlib.sha256(f"{document}\0{raw}\0{text_hash}".encode()).hexdigest()
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.documents (document_id, canonical_url) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING",
            (document, canonical_url),
        )
        cursor.execute(
            "INSERT INTO kx.raw_blobs (raw_sha256, compression, raw_bytes, stored_bytes, content)"
            " VALUES (%s, 'gzip', 4, 4, %s) ON CONFLICT DO NOTHING",
            (raw, b"body"),
        )
        cursor.execute(
            """
            INSERT INTO kx.document_versions (
                version_id, document_id, raw_sha256, source_kind, canonical_text,
                canonical_text_sha256, title, language, parser_name, parser_version,
                parser_config_sha256, quality, is_complete, fetched_at
            ) VALUES (%s, %s, %s, %s, 'text', %s, '', 'en', 'radar-kx', 'canonical-v4',
                      %s, 'trafilatura', true, %s)
            """,
            (version, document, raw, source_kind, text_hash, text_hash, NOW),
        )
    return version


def _insert_provenance(dsn: str, version: str, **fields: Any) -> None:
    columns = ["version_id", "recorded_by", *fields]
    values: list[Any] = [version, "test", *fields.values()]
    placeholders = ", ".join(["%s"] * len(columns))
    # The column list is written by this test, not by input; values stay bound.
    statement = f"INSERT INTO kx.version_provenance ({', '.join(columns)}) VALUES ({placeholders})"  # noqa: S608
    _execute(dsn, statement, tuple(values))


def test_migration_lands_on_the_repository_baseline(baseline_dsn: str) -> None:
    apply_migration_003(baseline_dsn)
    assert _scalar(baseline_dsn, "SELECT value FROM kx.metadata WHERE key='schema_version'") == 3


def test_migration_lands_on_the_drifted_production_schema(drifted_dsn: str) -> None:
    # Production carries an ALTER that the repository never had (defect D1). The
    # migration has to be idempotent against it, or the repository can never
    # describe production again.
    apply_migration_003(drifted_dsn)
    assert _scalar(drifted_dsn, "SELECT value FROM kx.metadata WHERE key='schema_version'") == 3
    definition = _scalar(
        drifted_dsn,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conname = 'document_versions_source_kind_check'",
    )
    for kind in ("operator_artifact", "network_browser_headers", "browser_render", "web_archive"):
        assert kind in definition


def test_both_baselines_end_at_the_same_schema(baseline_dsn: str, drifted_dsn: str) -> None:
    apply_migration_003(baseline_dsn)
    apply_migration_003(drifted_dsn)
    query = """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'kx'
        ORDER BY table_name, column_name
    """
    with connect(baseline_dsn) as first, first.cursor() as cursor:
        cursor.execute(query)
        left = cursor.fetchall()
    with connect(drifted_dsn) as second, second.cursor() as cursor:
        cursor.execute(query)
        right = cursor.fetchall()
    assert left == right


def test_the_new_acquisition_rungs_are_accepted(migrated_dsn: str) -> None:
    for kind in (
        "network_browser_headers",
        "browser_render",
        "web_archive",
        "operator_artifact",
        "local_import",
    ):
        _seed_version(migrated_dsn, source_kind=kind, url=f"https://example.com/{kind}")
    assert _scalar(migrated_dsn, "SELECT count(*) FROM kx.document_versions") == 5


def test_an_invented_source_kind_is_still_refused(migrated_dsn: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _seed_version(migrated_dsn, source_kind="vibes", url="https://example.com/vibes")


def test_provenance_is_append_only(migrated_dsn: str) -> None:
    version = _seed_version(migrated_dsn)
    _insert_provenance(migrated_dsn, version, source_access_method="http_default")
    with pytest.raises(psycopg.errors.RaiseException):
        _execute(migrated_dsn, "UPDATE kx.version_provenance SET notes = 'edited'")
    with pytest.raises(psycopg.errors.RaiseException):
        _execute(migrated_dsn, "DELETE FROM kx.version_provenance")


def test_an_archive_snapshot_is_citable_or_flagged(migrated_dsn: str) -> None:
    version = _seed_version(migrated_dsn)
    # Rule 19 of the evidence contract: a quotation points at the snapshot it came
    # from. Silently accepting an archive row with no snapshot identity is how a
    # quotation ends up pointing at a page that no longer says what we quoted.
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_provenance(
            migrated_dsn, version, source_access_method="web_archive", archive_used=True
        )
    _insert_provenance(
        migrated_dsn,
        version,
        source_access_method="web_archive",
        archive_used=True,
        manual_review_required=True,
        manual_review_reason="snapshot URL and capture date were not recorded",
    )
    _insert_provenance(
        migrated_dsn,
        version,
        source_access_method="web_archive",
        archive_used=True,
        archive_url="https://web.archive.org/web/20260101/https://example.com/a",
        archive_captured_at=NOW,
    )
    assert _scalar(migrated_dsn, "SELECT count(*) FROM kx.version_provenance") == 2


def test_handover_must_name_a_hand(migrated_dsn: str) -> None:
    version = _seed_version(migrated_dsn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_provenance(migrated_dsn, version, source_access_method="operator_file")
    _insert_provenance(
        migrated_dsn,
        version,
        source_access_method="operator_file",
        provided_by="ivan",
        provided_at=NOW,
    )


def test_current_provenance_is_the_latest_row(migrated_dsn: str) -> None:
    version = _seed_version(migrated_dsn)
    _insert_provenance(
        migrated_dsn,
        version,
        source_access_method="operator_file",
        provided_by="ivan",
        provided_at=NOW,
        notes="first",
    )
    _insert_provenance(
        migrated_dsn, version, source_access_method="browser_headers", notes="correction"
    )
    assert (
        _scalar(
            migrated_dsn,
            "SELECT source_access_method FROM kx.version_provenance_current WHERE version_id = %s",
            (version,),
        )
        == "browser_headers"
    )
    # The superseded row survives: "what did we believe, and when" stays answerable.
    assert _scalar(migrated_dsn, "SELECT count(*) FROM kx.version_provenance") == 2


def test_publication_blocks_default_to_no(migrated_dsn: str) -> None:
    unrecorded = _seed_version(migrated_dsn, url="https://example.com/unrecorded")
    ours = _seed_version(migrated_dsn, url="https://example.com/our-own-excerpt")
    clean = _seed_version(migrated_dsn, url="https://example.com/clean")
    _insert_provenance(
        migrated_dsn,
        ours,
        source_access_method="local_import",
        provided_by="pm",
        provided_at=NOW,
        manual_review_required=True,
        manual_review_reason="our excerpt of the source, not the source text",
    )
    _insert_provenance(migrated_dsn, clean, source_access_method="http_default")
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_id, block_reason FROM kx.version_publication_block")
        blocks = {str(row["version_id"]): row["block_reason"] for row in cursor.fetchall()}
    assert blocks == {
        unrecorded: "provenance_missing",
        ours: "provenance_manual_review",
    }
    assert clean not in blocks


def test_before_004_an_archive_without_a_snapshot_is_refused(schema3_dsn: str) -> None:
    # Kept as the record of what 004 changed: before it, an archive with no
    # snapshot identity was refused outright.
    archived = _seed_version(schema3_dsn, url="https://adopt.ai/blog/enterprise-ai-agents")
    _insert_provenance(
        schema3_dsn,
        archived,
        source_access_method="web_archive",
        archive_used=True,
        manual_review_required=True,
        manual_review_reason="snapshot URL and capture date were not recorded",
    )
    assert (
        _scalar(
            schema3_dsn,
            "SELECT block_reason FROM kx.version_publication_block WHERE version_id = %s",
            (archived,),
        )
        == "provenance_manual_review"
    )


def test_004_lands_on_top_of_003(schema3_dsn: str) -> None:
    _apply(schema3_dsn, (MIGRATION_004,))
    assert _scalar(schema3_dsn, "SELECT value FROM kx.metadata WHERE key='schema_version'") == 4


def test_an_archive_without_a_snapshot_is_a_caveat_not_a_refusal(caveat_dsn: str) -> None:
    # Owner decision 2026-08-22 (ADR-0004, rule 21a). The words are the source's; what
    # is missing is the reader's ability to re-check them at that snapshot. That is
    # a statement about the link, and the reader is told it.
    archived = _seed_version(caveat_dsn, url="https://adopt.ai/blog/enterprise-ai-agents")
    _insert_provenance(
        caveat_dsn,
        archived,
        source_access_method="web_archive",
        archive_used=True,
        manual_review_required=True,
        manual_review_reason="snapshot URL and capture date were not recorded",
    )
    with connect(caveat_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS count FROM kx.version_publication_block")
        assert one(cursor)["count"] == 0
        cursor.execute("SELECT caveat, caveat_detail FROM kx.version_publication_caveat")
        row = one(cursor)
        assert row["caveat"] == "archive_snapshot_not_recorded"
        assert "snapshot" in str(row["caveat_detail"])


def test_an_archive_with_its_snapshot_carries_no_caveat_at_all(caveat_dsn: str) -> None:
    archived = _seed_version(caveat_dsn, url="https://example.com/archived")
    _insert_provenance(
        caveat_dsn,
        archived,
        source_access_method="web_archive",
        archive_used=True,
        archive_url="https://web.archive.org/web/20260101/https://example.com/archived",
        archive_captured_at=NOW,
    )
    with connect(caveat_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS count FROM kx.version_publication_caveat")
        assert one(cursor)["count"] == 0
        cursor.execute("SELECT count(*) AS count FROM kx.version_publication_block")
        assert one(cursor)["count"] == 0


def test_the_canon_url_scheme_is_reserved_and_nothing_else_is(migrated_dsn: str) -> None:
    _execute(
        migrated_dsn,
        "INSERT INTO kx.documents (document_id, canonical_url) VALUES (%s, %s)",
        ("a" * 64, "agpm-canon:/white-paper-v1.2.md"),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        _execute(
            migrated_dsn,
            "INSERT INTO kx.documents (document_id, canonical_url) VALUES (%s, %s)",
            ("b" * 64, "file:///etc/passwd"),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        _execute(
            migrated_dsn,
            "INSERT INTO kx.documents (document_id, canonical_url) VALUES (%s, %s)",
            ("c" * 64, "agpm-canon:/"),
        )


def test_corpus_membership_class_defaults_to_radar_materials(migrated_dsn: str) -> None:
    _execute(
        migrated_dsn,
        "INSERT INTO kx.corpus_imports (corpus_sha256, source_name, row_count, document_count)"
        " VALUES (%s, 'materials.jsonl', 1, 1)",
        ("d" * 64,),
    )
    assert _scalar(migrated_dsn, "SELECT source_kind FROM kx.corpus_imports") == "radar_materials"
    _execute(
        migrated_dsn,
        "INSERT INTO kx.corpus_imports (corpus_sha256, source_name, row_count, document_count,"
        " source_kind) VALUES (%s, 'agpm-canon', 1, 1, 'canon_import')",
        ("e" * 64,),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        _execute(
            migrated_dsn,
            "INSERT INTO kx.corpus_imports (corpus_sha256, source_name, row_count,"
            " document_count, source_kind) VALUES (%s, 'x', 1, 1, 'whatever')",
            ("f" * 64,),
        )


def test_the_service_role_can_use_every_new_object(migrated_dsn: str) -> None:
    # 001 granted the service role out of band, so an object added later without a
    # GRANT is invisible to the deployed worker and the failure only shows up in
    # production.
    for table in (
        "version_provenance",
        "source_publication_policy",
        "egress_audit",
        "wiki_blobs",
        "wiki_snapshots",
        "wiki_snapshot_files",
        "store_reconciliation_reports",
    ):
        assert _scalar(
            migrated_dsn,
            "SELECT has_table_privilege('radar_kx', %s, 'INSERT')",
            (f"kx.{table}",),
        )
    for view in ("version_provenance_current", "version_publication_block"):
        assert _scalar(
            migrated_dsn, "SELECT has_table_privilege('radar_kx', %s, 'SELECT')", (f"kx.{view}",)
        )
    for sequence in (
        "version_provenance_provenance_id_seq",
        "egress_audit_egress_id_seq",
        "store_reconciliation_reports_report_id_seq",
    ):
        assert _scalar(
            migrated_dsn,
            "SELECT has_sequence_privilege('radar_kx', %s, 'USAGE')",
            (f"kx.{sequence}",),
        )


def test_wiki_snapshots_are_immutable_and_content_addressed(migrated_dsn: str) -> None:
    blob = hashlib.sha256(b"page").hexdigest()
    _execute(
        migrated_dsn,
        "INSERT INTO kx.wiki_blobs (blob_sha256, compression, raw_bytes, stored_bytes, content)"
        " VALUES (%s, 'gzip', 4, 4, %s)",
        (blob, b"page"),
    )
    _execute(
        migrated_dsn,
        "INSERT INTO kx.wiki_snapshots (snapshot_id, taken_at, manifest_sha256, perimeter,"
        " file_count, total_bytes, recorded_by)"
        " VALUES ('snap-1', %s, %s, 'agpm/**', 2, 8, 'test')",
        (NOW, hashlib.sha256(b"manifest").hexdigest()),
    )
    # Two paths, one blob: an unchanged page costs nothing on the next release.
    for path in ("agpm/wiki/principles/five.md", "agpm/wiki/principles/five-copy.md"):
        _execute(
            migrated_dsn,
            "INSERT INTO kx.wiki_snapshot_files (snapshot_id, relative_path, blob_sha256, bytes)"
            " VALUES ('snap-1', %s, %s, 4)",
            (path, blob),
        )
    assert (
        _scalar(migrated_dsn, "SELECT count(DISTINCT blob_sha256) FROM kx.wiki_snapshot_files") == 1
    )
    with pytest.raises(psycopg.errors.RaiseException):
        _execute(migrated_dsn, "UPDATE kx.wiki_snapshots SET perimeter = 'other'")


def test_egress_audit_is_immutable(migrated_dsn: str) -> None:
    _execute(
        migrated_dsn,
        "INSERT INTO kx.egress_audit (provider, model, purpose, payload_chars, payload_sha256,"
        " outcome, worker_release) VALUES ('zai', 'zai/glm-5.2', 'extraction', 120, %s,"
        " 'succeeded', 'test')",
        (hashlib.sha256(b"fragment").hexdigest(),),
    )
    with pytest.raises(psycopg.errors.RaiseException):
        _execute(migrated_dsn, "DELETE FROM kx.egress_audit")
