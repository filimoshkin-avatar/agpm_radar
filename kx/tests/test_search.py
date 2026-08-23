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
from radar_kx.search import (
    MATCH_MODES,
    RRF_K,
    SCOPES,
    SearchHit,
    build_hit,
    locate_snippet,
    search_sql,
)

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


def test_all_requires_every_term_and_any_does_not(stored: Database) -> None:
    # A quoted phrase wants every term. A question does not: conjoining fifteen
    # words finds nothing, and nothing found is not the same as nothing to find.
    query = "агентное управление проектами и совершенно посторонний термин"
    assert stored.search(query, scope="corpus", limit=10, match="all") == []
    assert stored.search(query, scope="corpus", limit=10, match="any")


def test_any_still_ranks_the_document_that_carries_most_of_the_query_first(
    stored: Database,
) -> None:
    hits = stored.search(
        "искусственный интеллект принимает решения", scope="corpus", limit=10, match="any"
    )
    assert hits
    assert hits[0].canonical_url in {"https://example.com/ru", "https://example.com/both"}


def test_an_unknown_match_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown match mode"):
        search_sql("corpus", match="fuzzy")


def test_both_match_modes_still_verify_their_offsets(stored: Database) -> None:
    for mode in MATCH_MODES:
        hits = stored.search("агентное управление", scope="corpus", limit=5, match=mode)
        assert hits
        assert all(hit.char_end > hit.char_start for hit in hits)


def test_documents_sharing_a_text_counts_documents_not_group_size_squared(
    migrated_dsn: str,
) -> None:
    # Joining a duplicate group back onto its own rows and summing the group size
    # gives n squared. On the production perimeter that reported 85 where the
    # answer is 11.
    database = Database(_settings(migrated_dsn))
    for index in range(3):
        _store(database, f"https://example.com/copy{index}", RUSSIAN)
    _store(database, "https://example.com/other", ENGLISH)
    scope = database.coverage_report()["scopes"]["corpus"]
    assert scope["documents"] == 4
    assert scope["distinct_texts"] == 2
    assert scope["documents_sharing_a_text"] == 3


# ---------------------------------------------------------------------------
# Stage 1: the hybrid retrieval that answers with labels
# ---------------------------------------------------------------------------


def test_the_hybrid_query_carries_all_three_arms_and_every_filter() -> None:
    from radar_kx.search import ARMS, FILTERS, evidence_sql

    sql = evidence_sql("corpus")
    assert "ranked_ru" in sql
    assert "ranked_en" in sql
    assert "ranked_meaning" in sql
    for arm in ARMS:
        assert arm in sql
    for name in FILTERS:
        assert f"%({name})s" in sql
    # Every filter is written so that NULL means "do not narrow", which is what
    # lets one query serve the filtered and the unfiltered question.
    assert sql.count("IS NULL OR") >= len(FILTERS) - 1


def test_the_semantic_arm_disappears_without_a_question_vector() -> None:
    from radar_kx.search import evidence_sql

    sql = evidence_sql("corpus")
    assert "%(question_vector)s::text IS NOT NULL" in sql


def test_every_bare_parameter_carries_a_cast() -> None:
    """PostgreSQL cannot type a placeholder that only appears beside IS NULL.

    Without the cast the whole query is refused with `could not determine data
    type`, and this is the one code path no test in the suite can reach through a
    database - it went out to production and came back as a 500.
    """
    from radar_kx.search import AGENT_SEARCH_SQL, FILTERS, evidence_sql

    for sql in (evidence_sql("corpus"), AGENT_SEARCH_SQL):
        for name in (*FILTERS, "question_vector"):
            assert f"%({name})s IS NULL" not in sql
            assert f"%({name})s IS NOT NULL" not in sql


def test_an_unknown_scope_is_refused_rather_than_defaulted() -> None:
    from radar_kx.search import evidence_sql

    with pytest.raises(ValueError, match="unknown search scope"):
        evidence_sql("everything")


def test_labels_survive_a_row_the_reading_pass_has_not_reached() -> None:
    from radar_kx.research import labels_of

    labels = labels_of({"claim_id": "c"})
    assert labels.material_kind is None
    assert labels.is_retelling is False
    assert labels.topics == ()
    assert labels.matched_by == ()


def test_labels_are_read_off_a_full_row() -> None:
    from radar_kx.research import labels_of

    labels = labels_of(
        {
            "material_kind": "forecast",
            "admission": "knowledge",
            "status": "observed_signal",
            "primary_source": "Gartner",
            "is_retelling": True,
            "shown_on": "2026-06-01",
            "shown_kind": "published",
            "topics": ["Пороги автономии"],
            "matched_by": ["слова", "смысл"],
        }
    )
    assert labels.material_kind == "forecast"
    assert labels.is_retelling is True
    assert labels.primary_source == "Gartner"
    assert labels.matched_by == ("слова", "смысл")


def test_a_numbered_element_shows_its_labels_to_the_reader() -> None:
    from radar_kx.research import build_package

    package = build_package(
        [
            {
                "claim_id": "c",
                "quote_text": "цитата",
                "source_url": "https://example.org/a",
                "char_start": 0,
                "char_end": 6,
                "relevance": 0.5,
                "material_kind": "fact",
                "admission": "knowledge",
                "status": "canon",
                "matched_by": ["смысл"],
            }
        ]
    )
    shown = package[0].as_json()
    assert shown["materialKind"] == "fact"
    assert shown["status"] == "canon"
    assert shown["matchedBy"] == ["смысл"]


def test_the_corpus_search_gains_a_meaning_arm_that_is_not_word_bound() -> None:
    """The point of embedding 19 851 chunks: a fragment sharing no word with the question.

    The lexical arms select from `matched`, which is by definition what the words
    found. The meaning arm reads the corpus instead, so restricting it to `matched`
    would have made the whole embedding run decorative.
    """
    sql = search_sql("corpus")
    assert "meaning_ranked" in sql
    assert "%(question_vector)s::text IS NOT NULL" in sql
    meaning = sql[sql.index("meaning_ranked AS (") : sql.index("reached AS (")]
    assert "FROM matched" not in meaning
    assert "kx.chunks" in meaning


def test_a_hit_says_whether_meaning_found_it() -> None:
    hit = build_hit(
        {
            "chunk_id": "c" * 64,
            "version_id": "v" * 64,
            "document_id": "d" * 64,
            "canonical_url": "https://example.org/a",
            "title": "Заголовок",
            "language": "ru",
            "rrf_score": 0.03,
            "ru_position": None,
            "en_position": None,
            "meaning_position": 4,
            "char_start": 10,
            "text": "Порог автономии определяет границу между классами решений.",
            "headline": "Порог автономии",
        }
    )
    assert hit.meaning_position == 4
    assert hit.as_json()["meaningPosition"] == 4
    # A hit no lexical arm reached is exactly what the meaning arm exists for.
    assert hit.ru_position is None and hit.en_position is None
