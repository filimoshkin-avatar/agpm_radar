"""Internal lexical search and the coverage report."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from conftest import connect, one
from radar_kx.canon_corpus import import_canon, scan_canon
from radar_kx.config import Settings
from radar_kx.database import Database, VersionProvenance
from radar_kx.parser import parse_content
from radar_kx.search import RRF_K, SCOPES, SearchHit, build_hit, locate_snippet, search_sql

NOW = datetime(2026, 8, 22, tzinfo=UTC)

RUSSIAN = (
    "Агентное управление проектами меняет роль руководителя. "
    "Искусственный интеллект принимает решения внутри рамок, заданных человеком. "
) * 8
ENGLISH = (
    "Agentic project management changes what a project manager does. "
    "Artificial intelligence decides inside a frame a human set. "
) * 8
BILINGUAL = RUSSIAN + "\n\n" + ENGLISH


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


def test_a_headline_that_is_present_verbatim_centres_the_snippet() -> None:
    text = "before the match, the match itself, after the match"
    snippet, offset, centred = locate_snippet(text, "the match itself")
    assert (snippet, offset, centred) == ("the match itself", 18, True)
    assert text[offset : offset + len(snippet)] == snippet


def test_a_headline_that_cannot_be_located_degrades_instead_of_lying() -> None:
    # ts_headline may collapse whitespace or drop a fragment. Offsets that do not
    # reproduce their own text would poison every span built on them, so a
    # headline that is not a verbatim run of the chunk is thrown away.
    text = "one two three four five"
    snippet, offset, centred = locate_snippet(text, "two ... four", fallback_chars=9)
    assert (snippet, offset, centred) == ("one two t", 0, False)
    assert text[offset : offset + len(snippet)] == snippet


def test_an_empty_headline_falls_back() -> None:
    snippet, offset, centred = locate_snippet("abcdef", "   ", fallback_chars=3)
    assert (snippet, offset, centred) == ("abc", 0, False)


def test_an_unknown_scope_is_refused_rather_than_silently_widened() -> None:
    with pytest.raises(ValueError, match="unknown search scope"):
        search_sql("everything")
    assert sorted(SCOPES) == ["canon", "corpus", "current", "historical"]


def _store(database: Database, url: str, body: str, *, source_kind: str = "local_import") -> None:
    parsed = parse_content(
        body=body.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        source_url=url,
        min_text_chars=200,
    )
    database.store_artifact_version(
        canonical_url=url,
        body=body.encode("utf-8"),
        parsed=parsed,
        source_kind=source_kind,
        fetched_at=NOW,
        provenance=VersionProvenance(
            source_access_method="local_import", provided_by="test", provided_at=NOW
        ),
        recorded_by="test",
    )


@pytest.fixture
def stored(migrated_dsn: str) -> Database:
    database = Database(_settings(migrated_dsn))
    _store(database, "https://example.com/ru", RUSSIAN)
    _store(database, "https://example.com/en", ENGLISH)
    _store(database, "https://example.com/both", BILINGUAL)
    return database


def test_search_finds_both_languages_and_reports_which_ranking_matched(
    stored: Database,
) -> None:
    hits = stored.search("искусственный интеллект", scope="corpus", limit=10)
    assert hits
    assert all(hit.ru_position is not None for hit in hits)
    english = stored.search("artificial intelligence", scope="corpus", limit=10)
    assert english
    assert all(hit.en_position is not None for hit in english)


def test_fusion_adds_reciprocal_ranks_and_orders_by_the_sum(stored: Database) -> None:
    hits = stored.search(
        "искусственный интеллект or artificial intelligence", scope="corpus", limit=20
    )
    assert hits
    for hit in hits:
        expected = 0.0
        if hit.ru_position is not None:
            expected += 1 / (RRF_K + hit.ru_position)
        if hit.en_position is not None:
            expected += 1 / (RRF_K + hit.en_position)
        assert hit.rrf_score == pytest.approx(expected)
        assert hit.ru_position is not None or hit.en_position is not None
    assert [hit.rrf_score for hit in hits] == sorted((hit.rrf_score for hit in hits), reverse=True)
    # A passage both rankings return outscores one that only one of them does,
    # which is the whole point of fusing instead of picking a language.
    both = [hit for hit in hits if hit.ru_position and hit.en_position]
    single = [hit for hit in hits if not (hit.ru_position and hit.en_position)]
    assert both
    if single:
        assert max(hit.rrf_score for hit in both) > max(hit.rrf_score for hit in single)


def test_every_snippet_reproduces_itself_from_its_offsets(
    stored: Database, migrated_dsn: str
) -> None:
    hits = stored.search("агентное управление проектами", scope="corpus", limit=10)
    assert hits
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        for hit in hits:
            cursor.execute(
                "SELECT substr(canonical_text, %s + 1, %s - %s) AS span"
                " FROM kx.document_versions WHERE version_id = %s",
                (hit.char_start, hit.char_end, hit.char_start, hit.version_id),
            )
            assert one(cursor)["span"] == hit.snippet


def test_a_snippet_whose_offsets_do_not_reproduce_it_is_refused(
    stored: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def shifted(row: dict[str, Any]) -> SearchHit:
        return replace(build_hit(row), char_start=build_hit(row).char_start + 7)

    monkeypatch.setattr("radar_kx.database.build_hit", shifted)
    with pytest.raises(RuntimeError, match="does not match its own offsets"):
        stored.search("агентное управление", scope="corpus", limit=5)


def test_scope_separates_the_membership_classes(migrated_dsn: str, tmp_path: Path) -> None:
    database = Database(_settings(migrated_dsn))
    _store(database, "https://example.com/ru", RUSSIAN)
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "agpm-манифест-v3-926ec919-b6bb-4058-a8d7-80b4053df422.md").write_text(
        RUSSIAN, encoding="utf-8"
    )
    import_canon(
        database,
        scan_canon(raw),
        source_name="agpm-canon",
        recorded_by="test",
        provided_by="pm",
    )
    canon = database.search("агентное управление", scope="canon", limit=10)
    corpus = database.search("агентное управление", scope="corpus", limit=10)
    current = database.search("агентное управление", scope="current", limit=10)
    assert {hit.canonical_url for hit in canon} == {
        "agpm-canon:/agpm-манифест-v3-926ec919-b6bb-4058-a8d7-80b4053df422.md"
    }
    assert len(corpus) > len(canon)
    # Nothing has been selected into an issue, so the current perimeter is empty -
    # and a search over it says so instead of quietly falling back to the corpus.
    assert current == []


def test_coverage_reports_each_class_separately(stored: Database) -> None:
    report = stored.coverage_report()
    assert sorted(report["scopes"]) == ["canon", "corpus", "current", "historical"]
    assert report["scopes"]["corpus"]["documents"] == 3
    assert report["scopes"]["corpus"]["complete_documents"] == 3
    assert report["scopes"]["canon"]["documents"] == 0
    assert report["scopes"]["current"]["documents"] == 0


def test_an_empty_perimeter_does_not_pass_the_completeness_gate(stored: Database) -> None:
    # Vacuous truth would report a green gate on a store that selected nothing.
    report = stored.coverage_report()
    assert report["gate"]["perimeterFullTextComplete"] is False
    assert report["status"] == "failed"


def test_the_smoke_floors_are_reported_with_the_counts_they_are_judged_against(
    stored: Database,
) -> None:
    report = stored.coverage_report()
    floors = {item["query"]: item for item in report["smoke"]}
    assert floors["искусственный интеллект"]["floor"] == 993
    assert floors["artificial intelligence"]["floor"] == 633
    # Three synthetic documents are nowhere near the production floor, and the
    # report says so rather than rescaling the expectation to what it found.
    assert all(item["ok"] is False for item in report["smoke"])
    assert all(item["chunks"] > 0 for item in report["smoke"])
