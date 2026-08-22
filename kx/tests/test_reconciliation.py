"""Comparing the file stores with KX, and recording what differs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import connect, one
from radar_kx.config import Settings
from radar_kx.database import Database, VersionProvenance
from radar_kx.parser import parse_content
from radar_kx.reconciliation import (
    MAX_LISTED,
    FileStoreEntry,
    ReconciliationError,
    compare,
    load_inventory,
    payload_sha256,
)

NOW_TEXT = "Agentic project management is a governance model. " * 40


def _entry(url: str, *, chars: int = 5_000, status: str = "resolved") -> FileStoreEntry:
    return FileStoreEntry(canonical_url=url, text_chars=chars, status=status)


def _kx(*urls: str, complete: set[str] | None = None) -> dict[str, dict[str, Any]]:
    complete = set(urls) if complete is None else complete
    return {
        _entry(url).document_id or url: {
            "canonicalUrl": url,
            "hasCompleteVersion": url in complete,
        }
        for url in urls
    }


def test_a_file_kx_has_never_seen_is_only_in_the_file_store() -> None:
    result = compare(
        "source_fulltext",
        [_entry("https://example.com/a"), _entry("https://example.com/b")],
        _kx("https://example.com/a"),
        source={},
    )
    assert result.only_in_file_store == ("https://example.com/b",)
    assert result.only_in_kx == ()
    assert result.differing == ()


def test_a_document_kx_holds_without_text_is_a_divergence_not_a_match() -> None:
    # This is the case that breaks a citation: we can read the text and cannot
    # cite it, and "the document exists" hides that.
    result = compare(
        "source_fulltext",
        [_entry("https://example.com/a", chars=16_026)],
        _kx("https://example.com/a", complete=set()),
        source={},
    )
    assert result.only_in_file_store == ()
    assert len(result.differing) == 1
    assert result.differing[0]["fileChars"] == 16_026
    assert "no complete version" in result.differing[0]["why"]


def test_the_registry_scope_does_not_ask_for_text() -> None:
    # A registry row records that something was seen. Whether KX also has its text
    # is a different question, and mixing them would report every unfetched
    # document as a divergence between the stores.
    result = compare(
        "discovery_registry",
        [_entry("https://example.com/a", chars=0)],
        _kx("https://example.com/a", complete=set()),
        source={},
    )
    assert result.differing == ()


def test_documents_only_kx_holds_are_named() -> None:
    result = compare(
        "source_fulltext",
        [_entry("https://example.com/a")],
        _kx("https://example.com/a", "https://example.com/kx-only"),
        source={},
    )
    assert result.only_in_kx == ("https://example.com/kx-only",)


def test_a_url_that_cannot_be_addressed_is_reported_not_silently_dropped() -> None:
    result = compare(
        "source_fulltext",
        [_entry("ftp://example.com/a"), _entry("https://example.com/b")],
        _kx("https://example.com/b"),
        source={},
    )
    assert result.unaddressable == ("ftp://example.com/a",)
    assert result.only_in_file_store == ()


def test_the_payload_caps_its_lists_but_never_its_counts() -> None:
    entries = [_entry(f"https://example.com/{index}") for index in range(MAX_LISTED + 50)]
    result = compare("source_fulltext", entries, {}, source={})
    payload = result.payload()
    assert len(result.only_in_file_store) == MAX_LISTED + 50
    assert len(payload["onlyInFileStore"]) == MAX_LISTED
    assert payload["truncated"]["onlyInFileStore"] == 50
    # The stored counts come from the result, not from the truncated list.
    assert result.as_json()["onlyInFileStore"] == MAX_LISTED + 50


def test_the_payload_hash_is_stable_across_key_order() -> None:
    left = {"scope": "source_fulltext", "a": 1, "b": [1, 2]}
    right = {"b": [1, 2], "scope": "source_fulltext", "a": 1}
    assert payload_sha256(left) == payload_sha256(right)


def test_an_unknown_scope_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"scope": "guesswork", "entries": []}), encoding="utf-8")
    with pytest.raises(ReconciliationError, match="scope must be one of"):
        load_inventory(path)


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


def test_the_report_is_recorded_and_immutable(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    url = "https://example.com/stored"
    parsed = parse_content(
        body=NOW_TEXT.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        source_url=url,
        min_text_chars=200,
    )
    from datetime import UTC, datetime

    database.store_artifact_version(
        canonical_url=url,
        body=NOW_TEXT.encode("utf-8"),
        parsed=parsed,
        source_kind="local_import",
        fetched_at=datetime(2026, 8, 22, tzinfo=UTC),
        provenance=VersionProvenance(
            source_access_method="local_import",
            provided_by="test",
            provided_at=datetime(2026, 8, 22, tzinfo=UTC),
        ),
        recorded_by="test",
    )
    outcome = database.record_store_reconciliation(
        "source_fulltext",
        [_entry(url), _entry("https://example.com/never-imported")],
        source={"root": "knowledge/agpm-radar/data/source-fulltext"},
        generated_by="test",
    )
    assert outcome["fileStoreCount"] == 2
    assert outcome["onlyInFileStore"] == 1
    assert outcome["differing"] == 0
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT scope, only_in_file_store, payload, payload_sha256"
            " FROM kx.store_reconciliation_reports"
        )
        row = one(cursor)
        assert row["scope"] == "source_fulltext"
        assert row["only_in_file_store"] == 1
        assert row["payload"]["onlyInFileStore"] == ["https://example.com/never-imported"]
        assert row["payload_sha256"] == payload_sha256(row["payload"])
        with pytest.raises(Exception, match="immutable|reject"):
            cursor.execute("UPDATE kx.store_reconciliation_reports SET scope = 'edited'")


def test_the_fulltext_scope_ignores_documents_kx_could_not_fetch(migrated_dsn: str) -> None:
    # 2334 documents in production have no complete version. Counting them as
    # "only in KX" against a 75-file text cache would bury the real finding under
    # a known and separate problem.
    database = Database(_settings(migrated_dsn))
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.documents (document_id, canonical_url) VALUES (%s, %s)",
            ("f" * 64, "https://example.com/never-fetched"),
        )
    outcome = database.record_store_reconciliation(
        "source_fulltext", [], source={}, generated_by="test"
    )
    assert outcome["kxCount"] == 0
    assert outcome["onlyInKx"] == 0
