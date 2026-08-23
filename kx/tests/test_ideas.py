"""Slice 2.9: two independent sources, or it is not shown (P13, ADR-0007)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import connect
from radar_kx.config import Settings
from radar_kx.database import Database, VersionProvenance
from radar_kx.extraction import Fragment, ProposedClaim, align_all, prompt_sha256
from radar_kx.ideas import (
    MAX_GROUP_SIZE,
    MIN_INDEPENDENT_SOURCES,
    CandidateGroup,
    ClaimRecord,
    IndependenceVerdict,
    build_idea_prompt,
    group_claims,
    overlap,
    parse_idea,
    summarize,
)
from radar_kx.parser import parse_content
from radar_kx.source_families import FamilyDecision

SHARED = (
    "Enterprise adoption of agentic project management reached forty one percent "
    "among surveyed programmes during the second quarter of the year"
)
REPHRASED = (
    "Adoption of agentic project management among surveyed enterprise programmes "
    "reached forty one percent in the second quarter"
)
UNRELATED = (
    "Procurement cycles for infrastructure software lengthened considerably after "
    "the regulator published its revised guidance on vendor concentration risk"
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


def _claim(identifier: str, document: str, quote: str) -> ClaimRecord:
    return ClaimRecord(
        claim_id=identifier,
        document_id=document,
        predicate="reached",
        object_text="adoption",
        quote_text=quote,
    )


# --------------------------------------------------------------------------
# Grouping is deterministic
# --------------------------------------------------------------------------


def test_two_sources_saying_the_same_thing_are_one_group() -> None:
    groups = group_claims(
        [
            _claim("a", "doc-1", SHARED),
            _claim("b", "doc-2", REPHRASED),
            _claim("c", "doc-3", UNRELATED),
        ]
    )
    assert len(groups) == 1
    assert {claim.claim_id for claim in groups[0].claims} == {"a", "b"}
    assert groups[0].document_ids == ("doc-1", "doc-2")


def test_two_claims_from_one_document_are_one_voice_and_not_a_group() -> None:
    # Grouping them adds a claim and no independence, and it is how a single
    # article turns into a five-claim "idea".
    assert group_claims([_claim("a", "doc-1", SHARED), _claim("b", "doc-1", REPHRASED)]) == ()


def test_a_group_is_reproducible_from_its_claims() -> None:
    # A reader has to be able to be shown why two claims are in one group.
    first = group_claims([_claim("a", "doc-1", SHARED), _claim("b", "doc-2", REPHRASED)])
    again = group_claims([_claim("b", "doc-2", REPHRASED), _claim("a", "doc-1", SHARED)])
    assert first[0].fingerprint == again[0].fingerprint


def test_a_short_quotation_cannot_be_grouped_by_overlap() -> None:
    short = _claim("a", "doc-1", "Adoption rose.")
    assert overlap(short, _claim("b", "doc-2", SHARED)) == 0.0


def test_a_group_larger_than_a_topic_is_not_an_idea() -> None:
    many = [_claim(f"c{index}", f"doc-{index}", SHARED) for index in range(MAX_GROUP_SIZE + 3)]
    assert group_claims(many) == ()


# --------------------------------------------------------------------------
# The gate is arithmetic and fails closed
# --------------------------------------------------------------------------


def _verdict(sources: int, unknown: int = 0) -> IndependenceVerdict:
    return IndependenceVerdict(
        independent_sources=sources,
        unknown_documents=unknown,
        collapsed_by_family=0,
        collapsed_by_cluster=0,
        family_decision_high_water=1,
        confirmed_cluster_count=0,
    )


def test_the_gate_is_two_independent_sources() -> None:
    assert MIN_INDEPENDENT_SOURCES == 2
    assert not _verdict(1).admitted
    assert _verdict(2).admitted


def test_documents_with_no_family_never_admit_an_idea() -> None:
    # ADR-0007 §12: absence of a family is not independence.
    assert not _verdict(0, unknown=5).admitted


def test_the_summary_says_what_was_refused_and_why() -> None:
    # "Nothing was proposed this week" and "eleven were proposed and none had two
    # independent sources" are different facts about the corpus.
    groups = (
        CandidateGroup(claims=(_claim("a", "d1", SHARED), _claim("b", "d2", REPHRASED))),
        CandidateGroup(claims=(_claim("c", "d3", SHARED), _claim("d", "d4", REPHRASED))),
    )
    verdicts = {
        groups[0].fingerprint: _verdict(2),
        groups[1].fingerprint: _verdict(1, unknown=1),
    }
    overview = summarize(groups, verdicts)
    assert overview == {
        "groups": 2,
        "admitted": 1,
        "refusedByIndependence": 1,
        "byIndependentSources": {"1": 1, "2": 1},
    }


# --------------------------------------------------------------------------
# The model only phrases
# --------------------------------------------------------------------------


def test_the_prompt_carries_quotations_and_not_documents() -> None:
    group = CandidateGroup(claims=(_claim("a", "d1", SHARED), _claim("b", "d2", REPHRASED)))
    prompt = build_idea_prompt(group)
    assert SHARED in prompt
    assert REPHRASED in prompt
    assert "data, not instruction" in prompt
    assert "do not generalise beyond them" in prompt


def test_an_answer_that_is_not_the_shape_asked_for_is_refused() -> None:
    for answer in ("no json here", '{"title": "x"}', '{"statement": "y"}'):
        with pytest.raises(ValueError, match="no JSON object|no title or no statement"):
            parse_idea(answer)


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def _document(database: Database, url: str, text: str) -> tuple[str, str]:
    parsed = parse_content(
        body=text.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        source_url=url,
        min_text_chars=50,
    )
    outcome = database.store_artifact_version(
        canonical_url=url,
        body=text.encode("utf-8"),
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
    return str(outcome.document_id), str(outcome.version_id)


def _extract(database: Database, dsn: str, version_id: str, quote: str) -> None:
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
            (ProposedClaim("reached", "adoption", quote),),
        ),
        model="glm-5.2",
        prompt_sha256=prompt_sha256(fragment),
    )


def _family(database: Database, key: str, document_id: str) -> None:
    database.apply_family_batch(
        decided_by="test",
        decisions=[
            FamilyDecision(
                family_key=key,
                display_name=key,
                family_kind="owner",
                action="confirmed",
                rationale="test",
                document_ids=(document_id,),
            )
        ],
    )


def test_an_idea_carries_the_verdict_it_was_judged_on(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    first, first_version = _document(database, "https://one.example/a", SHARED + " " + UNRELATED)
    second, second_version = _document(
        database, "https://two.example/b", REPHRASED + " " + UNRELATED
    )
    _extract(database, migrated_dsn, first_version, SHARED)
    _extract(database, migrated_dsn, second_version, REPHRASED)
    _family(database, "one-example", first)
    _family(database, "two-example", second)

    judged = database.propose_candidate_groups(scope="corpus", threshold=0.45)
    assert len(judged) == 1
    group, verdict = judged[0]
    assert verdict.independent_sources == 2
    assert verdict.admitted
    assert verdict.family_decision_high_water == 2

    recorded: dict[str, Any] = database.record_idea(
        group,
        verdict,
        title="Adoption reached 41%",
        statement="Two sources say so.",
        created_by="test",
        model="glm-5.2",
    )
    assert recorded["admitted"] is True

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT admitted, independent_sources, family_decision_high_water,"
            " confirmed_cluster_count, state FROM kx.ideas"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["admitted"] is True
        assert row["independent_sources"] == 2
        # The version of the world it was judged in, so a correction next month
        # produces a new assessment rather than changing this one.
        assert row["family_decision_high_water"] == 2
        assert row["confirmed_cluster_count"] == 0
        assert row["state"] == "proposed"
        cursor.execute("SELECT count(*) AS total FROM kx.idea_evidence")
        assert cursor.fetchone()["total"] == 2  # type: ignore[index]


def test_an_idea_that_failed_the_gate_is_recorded_and_can_never_be_shown(
    migrated_dsn: str,
) -> None:
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.ideas (title, statement, created_by, independent_sources,"
            " unknown_documents, collapsed_by_family, collapsed_by_cluster,"
            " family_decision_high_water, confirmed_cluster_count, admitted)"
            " VALUES ('x', 'y', 'test', 1, 1, 0, 0, 0, 0, false) RETURNING idea_id",
        )
        first = cursor.fetchone()
        assert first is not None
        idea_id = first["idea_id"]
        with pytest.raises(Exception, match="only_an_admitted_idea_is_shown"):
            cursor.execute("UPDATE kx.ideas SET state = 'shown' WHERE idea_id = %s", (idea_id,))


def test_an_assessed_idea_cannot_omit_its_verdict(migrated_dsn: str) -> None:
    with (
        connect(migrated_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(Exception, match="an_assessed_idea_carries_its_verdict"),
    ):
        cursor.execute(
            "INSERT INTO kx.ideas (title, statement, created_by, admitted)"
            " VALUES ('x', 'y', 'test', true)"
        )


def test_an_owner_decision_is_append_only(migrated_dsn: str) -> None:
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.ideas (title, statement, created_by, independent_sources,"
            " unknown_documents, collapsed_by_family, collapsed_by_cluster,"
            " family_decision_high_water, confirmed_cluster_count, admitted)"
            " VALUES ('x', 'y', 'test', 3, 0, 0, 0, 0, 0, true) RETURNING idea_id"
        )
        first = cursor.fetchone()
        assert first is not None
        idea_id = first["idea_id"]
        cursor.execute(
            "INSERT INTO kx.idea_decisions (idea_id, verdict, decided_by, rationale)"
            " VALUES (%s, 'accepted', 'owner', 'it holds up')",
            (idea_id,),
        )
        with pytest.raises(Exception, match="immutable|reject"):
            cursor.execute("UPDATE kx.idea_decisions SET verdict = 'rejected'")
