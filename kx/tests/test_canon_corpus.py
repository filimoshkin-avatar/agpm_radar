"""The AgPM canon as its own corpus: identity, fidelity, and what may be quoted."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import connect, one
from radar_kx.canon_corpus import (
    CANON_FIDELITY,
    CanonCorpusError,
    build_canon_artifact,
    canon_corpus_sha256,
    canon_summary,
    import_canon,
    scan_canon,
)
from radar_kx.config import Settings
from radar_kx.database import Database

AGPM_RAW = Path("/root/.openclaw-projectmanager/workspace/knowledge/agpm/raw")

BODY = "# White paper\n\n" + ("Agentic project management is a governance model. " * 40)


def _settings(dsn: str) -> Settings:
    return Settings(
        dsn=dsn,
        release_id="test",
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


@pytest.fixture
def canon_directory(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    raw.mkdir()
    for stem, fidelity in (
        ("agpm-white-paper-v1.2-a565755c-bfa2-4255-97cf-de5b43d25625", "full_text"),
        ("iso-21502-2024-classical-reference-extract", "extract"),
        ("gost-r-72514-2026-public-reference-note-2026-05-02", "note"),
    ):
        assert CANON_FIDELITY[stem] == fidelity
        (raw / f"{stem}.md").write_text(BODY, encoding="utf-8")
    return raw


def test_an_undeclared_canon_file_stops_the_import(canon_directory: Path) -> None:
    # A new file in raw/ is a decision about what may be quoted, so it cannot be
    # imported until somebody says what it is.
    (canon_directory / "something-new.md").write_text(BODY, encoding="utf-8")
    with pytest.raises(CanonCorpusError, match="no declared fidelity"):
        scan_canon(canon_directory)


def test_an_empty_canon_directory_is_an_error_not_an_empty_success(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(CanonCorpusError, match="no canon markdown"):
        scan_canon(empty)


def test_canon_documents_carry_the_reserved_identity(canon_directory: Path) -> None:
    files = scan_canon(canon_directory)
    assert len(files) == 3
    for item in files:
        assert item.canonical_url.startswith("agpm-canon:/")
        assert item.material_id.startswith("canon:")


def test_only_a_faithful_conversion_is_quotable(canon_directory: Path) -> None:
    summary = canon_summary(scan_canon(canon_directory))
    assert summary["byFidelity"] == {"extract": 1, "full_text": 1, "note": 1}
    assert summary["quotable"] == 1
    assert summary["blockedFromQuotation"] == [
        "gost-r-72514-2026-public-reference-note-2026-05-02.md",
        "iso-21502-2024-classical-reference-extract.md",
    ]


def test_the_block_says_why_and_the_full_text_carries_no_block(canon_directory: Path) -> None:
    manifest = build_canon_artifact(
        scan_canon(canon_directory),
        name="agpm-canon",
        recorded_by="test",
        provided_by="project-manager",
    )
    by_fidelity = {document.canonical_url: document.provenance for document in manifest.documents}
    white_paper = next(
        provenance for url, provenance in by_fidelity.items() if "white-paper" in url
    )
    excerpt = next(provenance for url, provenance in by_fidelity.items() if "iso-21502" in url)
    assert white_paper.manual_review_required is False
    assert excerpt.manual_review_required is True
    assert "not the text of the source" in str(excerpt.manual_review_reason)
    # Every canon document is a local file, and a local import has to name a hand.
    for provenance in by_fidelity.values():
        assert provenance.source_access_method == "local_import"
        assert provenance.provided_by == "project-manager"
        assert provenance.provided_at is not None


def test_the_corpus_hash_follows_the_content(canon_directory: Path) -> None:
    before = canon_corpus_sha256(scan_canon(canon_directory))
    (canon_directory / "iso-21502-2024-classical-reference-extract.md").write_text(
        BODY + "\nOne more sentence.\n", encoding="utf-8"
    )
    assert canon_corpus_sha256(scan_canon(canon_directory)) != before


def test_the_declared_fidelity_table_matches_the_canon_on_disk() -> None:
    if not AGPM_RAW.exists():
        pytest.skip("the AgPM canon is not present on this host")
    on_disk = {path.stem for path in AGPM_RAW.glob("*.md")}
    declared = set(CANON_FIDELITY)
    assert on_disk - declared == set(), "canon files with no declared fidelity"
    assert declared - on_disk == set(), "declared fidelity for files that are gone"


def test_import_registers_its_own_membership_class_and_queues_nothing(
    migrated_dsn: str, canon_directory: Path
) -> None:
    database = Database(_settings(migrated_dsn))
    result = import_canon(
        database,
        scan_canon(canon_directory),
        source_name="agpm-canon",
        recorded_by="test",
        provided_by="project-manager",
    )
    assert result["corpus"]["sourceKind"] == "canon_import"
    assert result["import"]["versionsCreated"] == 3
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT source_kind, row_count, document_count FROM kx.corpus_imports")
        row = one(cursor)
        assert (row["source_kind"], row["row_count"], row["document_count"]) == (
            "canon_import",
            3,
            3,
        )
        # A canon document has no web address. Queueing one would hand the fetcher a
        # URL it cannot parse, and it would fail for as long as the row exists.
        cursor.execute("SELECT count(*) AS count FROM kx.fetch_queue")
        assert one(cursor)["count"] == 0
        cursor.execute("SELECT count(*) AS count FROM kx.fetch_attempts")
        assert one(cursor)["count"] == 0
        cursor.execute("SELECT count(*) AS count FROM kx.version_publication_block")
        assert one(cursor)["count"] == 2
        cursor.execute("SELECT DISTINCT source_kind FROM kx.document_versions")
        assert one(cursor)["source_kind"] == "local_import"


def test_reimporting_the_canon_changes_nothing(migrated_dsn: str, canon_directory: Path) -> None:
    database = Database(_settings(migrated_dsn))
    files = scan_canon(canon_directory)
    import_canon(database, files, source_name="agpm-canon", recorded_by="test", provided_by="pm")
    again = import_canon(
        database, files, source_name="agpm-canon", recorded_by="test", provided_by="pm"
    )
    assert again["import"]["versionsCreated"] == 0
    assert again["import"]["versionsAlreadyPresent"] == 3
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS count FROM kx.version_provenance")
        assert one(cursor)["count"] == 3
        cursor.execute("SELECT count(*) AS count FROM kx.corpus_imports")
        assert one(cursor)["count"] == 1


def test_the_canon_never_lands_in_a_radar_coverage_denominator(
    migrated_dsn: str, canon_directory: Path
) -> None:
    database = Database(_settings(migrated_dsn))
    import_canon(
        database,
        scan_canon(canon_directory),
        source_name="agpm-canon",
        recorded_by="test",
        provided_by="pm",
    )
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        # The perimeter is the denominator for Radar materials, and the canon is not
        # in it. Coverage is computed per membership class, never over their union.
        cursor.execute("SELECT count(*) AS count FROM kx.issue_perimeter_members")
        assert one(cursor)["count"] == 0
        cursor.execute(
            "SELECT count(*) AS count FROM kx.documents WHERE canonical_url LIKE 'agpm-canon:%'"
        )
        assert one(cursor)["count"] == 3


def test_radar_materials_may_not_be_registered_through_the_canon_path(
    migrated_dsn: str,
) -> None:
    database = Database(_settings(migrated_dsn))
    with pytest.raises(ValueError, match="import_manifest"):
        database.register_corpus_members(
            corpus_sha256="a" * 64,
            source_name="materials.jsonl",
            source_kind="radar_materials",
            members=[],
        )
