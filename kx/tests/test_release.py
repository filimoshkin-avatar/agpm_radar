"""Slice 3.1: the published slice, the pointer, and the way back."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import connect
from radar_kx.config import Settings
from radar_kx.database import Database, VersionProvenance
from radar_kx.extraction import Fragment, ProposedClaim, align_all, prompt_sha256
from radar_kx.parser import parse_content
from radar_kx.release import AUDIENCES, ReleaseError, compose, reconcile

PARAGRAPH = (
    "Adoption of agentic project management at Deloitte reached 41% in 2026, up "
    "from a much smaller base the year before."
)
DOCUMENT = f"{PARAGRAPH}\n\nA second paragraph so the first one has a boundary."


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


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------


def _composition(**overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "quotes": [
            {
                "quote_id": "q1",
                "original_text": PARAGRAPH,
                "translated_text": None,
                "attribution": "https://example.com/a",
                "caveat": None,
            }
        ],
        "concepts": [{"concept_id": "c1", "body": "# A page\n\nIts words."}],
        "statements": [{"statement_id": "s1", "statement": "One thing.", "confirmed_evidence": 0}],
        "ideas": [
            {
                "idea_id": "i1",
                "title": "T",
                "statement": "S",
                "independent_sources": 2,
            }
        ],
        "wiki_snapshot_id": "wiki-agpm-abc",
        "graph_snapshot_id": "graph-def",
        "family_decision_high_water": 7,
    }
    arguments.update(overrides)
    return compose(**arguments)


def test_the_same_slice_is_the_same_release() -> None:
    assert _composition().release_id == _composition().release_id


def test_a_statement_that_gained_evidence_is_a_different_release() -> None:
    # Its words did not move, and it is still a different thing to publish.
    before = _composition()
    after = _composition(
        statements=[{"statement_id": "s1", "statement": "One thing.", "confirmed_evidence": 1}]
    )
    assert before.release_id != after.release_id


def test_the_inputs_are_part_of_the_identity() -> None:
    # A release built against a different wiki is a different release even when
    # every element happens to match.
    assert _composition().release_id != _composition(wiki_snapshot_id="wiki-agpm-other").release_id
    assert _composition().release_id != _composition(family_decision_high_water=8).release_id


def test_the_first_version_has_one_audience_and_the_column_exists_anyway() -> None:
    # ADR-0006 §1.4: the service checks it from day one. A check added later is a
    # check that was missing in between.
    assert AUDIENCES == ("public", "editor")


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def test_reconciliation_separates_three_different_things() -> None:
    result = reconcile(
        "kb-x",
        active=True,
        published={"quote:a": "1", "quote:b": "2", "quote:withdrawn": "3"},
        current={"quote:a": "1", "quote:b": "CHANGED", "quote:new": "4"},
    )
    assert result.missing_from_slice == ("quote:new",)
    assert result.absent_from_source == ("quote:withdrawn",)
    assert result.changed == ("quote:b",)
    assert not result.identical


def test_an_untouched_release_reconciles_clean() -> None:
    same = {"quote:a": "1"}
    assert reconcile("kb-x", active=True, published=same, current=dict(same)).identical


# --------------------------------------------------------------------------
# The pointer
# --------------------------------------------------------------------------


def _publishable(database: Database, dsn: str, url: str) -> None:
    parsed = parse_content(
        body=DOCUMENT.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        source_url=url,
        min_text_chars=50,
    )
    outcome = database.store_artifact_version(
        canonical_url=url,
        body=DOCUMENT.encode("utf-8"),
        parsed=parsed,
        source_kind="local_import",
        fetched_at=datetime(2026, 8, 23, tzinfo=UTC),
        provenance=VersionProvenance(
            source_access_method="local_import",
            provided_by="test",
            provided_at=datetime(2026, 8, 23, tzinfo=UTC),
        ),
        recorded_by="test",
    )
    version_id = str(outcome.version_id)
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT chunk_id, char_start, char_end, text FROM kx.chunks"
            " WHERE version_id = %s ORDER BY ordinal LIMIT 1",
            (version_id,),
        )
        row = cursor.fetchone()
        assert row is not None
    fragment = Fragment(
        version_id=version_id,
        chunk_id=str(row["chunk_id"]),
        char_start=int(cast(int, row["char_start"])),
        char_end=int(cast(int, row["char_end"])),
        text=str(row["text"]),
    )
    database.record_extraction(
        fragment,
        align_all(
            fragment,
            database.canonical_text(version_id),
            (ProposedClaim("reached", "adoption", PARAGRAPH),),
        ),
        model="glm-5.2",
        prompt_sha256=prompt_sha256(fragment),
    )
    database.publish_quotes(scope="corpus", target_language="en")


def test_a_release_is_built_once_and_recognised_the_second_time(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    _publishable(database, migrated_dsn, "https://example.com/a")
    first: dict[str, Any] = database.build_release(built_by="test")
    assert first["alreadyBuilt"] is False
    assert first["quotes"] == 1
    second: dict[str, Any] = database.build_release(built_by="test")
    assert second["alreadyBuilt"] is True
    assert second["releaseId"] == first["releaseId"]


def test_publishing_moves_one_pointer_and_records_who(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    _publishable(database, migrated_dsn, "https://example.com/a")
    built: dict[str, Any] = database.build_release(built_by="test")
    database.publish_release(str(built["releaseId"]), actor="owner", rationale="the first slice")
    active = database.active_release()
    assert active is not None
    assert active["release_id"] == built["releaseId"]
    assert active["switched_by"] == "owner"
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS total FROM kb.active_release")
        assert cursor.fetchone()["total"] == 1  # type: ignore[index]
        cursor.execute("SELECT action, actor FROM kx.knowledge_release_events ORDER BY event_id")
        assert [row["action"] for row in cursor.fetchall()] == ["built", "published"]


def test_a_release_that_was_never_built_cannot_be_published(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    with pytest.raises(ReleaseError, match="has not been built"):
        database.publish_release("kb-nothing", actor="owner", rationale="x")


def test_rolling_back_is_the_same_pointer_going_the_other_way(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    _publishable(database, migrated_dsn, "https://example.com/a")
    first: dict[str, Any] = database.build_release(built_by="test")
    database.publish_release(str(first["releaseId"]), actor="owner", rationale="first")

    _publishable(database, migrated_dsn, "https://example.com/b")
    second: dict[str, Any] = database.build_release(built_by="test")
    assert second["releaseId"] != first["releaseId"]
    database.publish_release(str(second["releaseId"]), actor="owner", rationale="second")

    rolled: dict[str, Any] = database.rollback_release(
        actor="owner", rationale="the second one was wrong"
    )
    assert rolled["releaseId"] == first["releaseId"]
    active = database.active_release()
    assert active is not None
    assert active["release_id"] == first["releaseId"]

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT action FROM kx.knowledge_release_events ORDER BY event_id")
        actions = [row["action"] for row in cursor.fetchall()]
    # A rollback that left no trace would make the event log a story about what
    # was meant to happen.
    assert actions == ["built", "published", "built", "published", "superseded", "rolled_back"]


def test_there_is_nothing_to_roll_back_to_after_one_release(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    _publishable(database, migrated_dsn, "https://example.com/a")
    built: dict[str, Any] = database.build_release(built_by="test")
    database.publish_release(str(built["releaseId"]), actor="owner", rationale="first")
    with pytest.raises(ReleaseError, match="no earlier release"):
        database.rollback_release(actor="owner", rationale="x")


def test_a_release_and_its_events_cannot_be_rewritten(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    _publishable(database, migrated_dsn, "https://example.com/a")
    built: dict[str, Any] = database.build_release(built_by="test")
    database.publish_release(str(built["releaseId"]), actor="owner", rationale="first")
    for statement in (
        "UPDATE kx.knowledge_releases SET quote_count = 0",
        "UPDATE kx.knowledge_release_events SET actor = 'somebody else'",
        "DELETE FROM kx.knowledge_release_events",
    ):
        with (
            connect(migrated_dsn) as connection,
            connection.cursor() as cursor,
            pytest.raises(Exception, match="immutable|reject"),
        ):
            cursor.execute(statement)


def test_the_slice_carries_only_what_was_already_confirmed(migrated_dsn: str) -> None:
    # An idea that failed the independence gate is recorded in kx and must not
    # reach the slice.
    database = Database(_settings(migrated_dsn))
    _publishable(database, migrated_dsn, "https://example.com/a")
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.ideas (title, statement, created_by, independent_sources,"
            " unknown_documents, collapsed_by_family, collapsed_by_cluster,"
            " family_decision_high_water, confirmed_cluster_count, admitted)"
            " VALUES ('refused', 'y', 'test', 1, 1, 0, 0, 0, 0, false)"
        )
        cursor.execute(
            "INSERT INTO kx.ideas (title, statement, created_by, independent_sources,"
            " unknown_documents, collapsed_by_family, collapsed_by_cluster,"
            " family_decision_high_water, confirmed_cluster_count, admitted)"
            " VALUES ('admitted', 'y', 'test', 3, 0, 0, 0, 0, 0, true)"
        )
    built: dict[str, Any] = database.build_release(built_by="test")
    assert built["ideas"] == 1
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT title FROM kb.ideas")
        assert [row["title"] for row in cursor.fetchall()] == ["admitted"]


def test_reconciliation_sees_the_store_moving_under_a_published_release(
    migrated_dsn: str,
) -> None:
    # A published release is expected to fall behind: it is a snapshot and the
    # store keeps moving. What must never happen is not knowing.
    database = Database(_settings(migrated_dsn))
    _publishable(database, migrated_dsn, "https://example.com/a")
    built: dict[str, Any] = database.build_release(built_by="test")
    database.publish_release(str(built["releaseId"]), actor="owner", rationale="first")
    assert database.reconcile_release()["identical"] is True

    _publishable(database, migrated_dsn, "https://example.com/b")
    after: dict[str, Any] = database.reconcile_release()
    assert after["identical"] is False
    assert after["missingFromSlice"] == 1
    assert after["absentFromSource"] == 0


def test_reconciliation_compares_all_four_kinds(migrated_dsn: str) -> None:
    # The first production reconciliation reported 84 elements missing from a
    # slice published thirty seconds earlier, because it read quotes and
    # statements out of kb and compared them against all four kinds. A comparison
    # against half a side is not a comparison.
    database = Database(_settings(migrated_dsn))
    _publishable(database, migrated_dsn, "https://example.com/a")
    built: dict[str, Any] = database.build_release(built_by="test")
    database.publish_release(str(built["releaseId"]), actor="owner", rationale="first")
    result: dict[str, Any] = database.reconcile_release()
    assert result["identical"] is True
    assert result["missingFromSlice"] == 0
