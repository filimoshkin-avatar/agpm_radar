"""Slice 2.4: who counts as a separate observer, and who only looks like one."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from conftest import connect
from radar_kx.config import Settings
from radar_kx.database import Database, VersionProvenance
from radar_kx.duplicates import (
    MIN_SHINGLES,
    DocumentText,
    find_hash_clusters,
    find_shingle_clusters,
    jaccard,
    shingles,
)
from radar_kx.parser import parse_content
from radar_kx.source_families import (
    DocumentHost,
    FamilyBatchError,
    load_family_batch,
    propose_families,
    registrable_domain,
)

# Varied on purpose. A shingle bag is a set, so forty copies of one sentence produce
# a handful of shingles and fall under MIN_SHINGLES - which is the guard working, not
# the corpus this test means to describe.
BODY = " ".join(
    f"Sentence {index} says an agentic run assigns accountability number {index} "
    f"to a named human owner reviewing outcome {index * 7} before release."
    for index in range(60)
)


UNRELATED = " ".join(
    f"Paragraph {index} discusses procurement cycle {index} and vendor lock-in "
    f"risk {index * 3} in unrelated infrastructure programmes."
    for index in range(60)
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


def _store(database: Database, url: str, text: str) -> str:
    parsed = parse_content(
        body=text.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        source_url=url,
        min_text_chars=200,
    )
    outcome = database.store_artifact_version(
        canonical_url=url,
        body=text.encode("utf-8"),
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
    return str(outcome.document_id)


def _batch(path: Path, families: list[dict[str, Any]], *, decided_by: str = "owner") -> Path:
    path.write_text(json.dumps({"decidedBy": decided_by, "families": families}), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Grouping hosts
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("www.mckinsey.com", "mckinsey.com"),
        ("mckinsey.com", "mckinsey.com"),
        ("news.bbc.co.uk", "bbc.co.uk"),
        ("a.b.c.example.com.au", "example.com.au"),
        ("example.org", "example.org"),
    ],
)
def test_a_host_groups_by_who_runs_it(host: str, expected: str) -> None:
    assert registrable_domain(host) == expected


def test_the_proposal_carries_the_evidence_that_produced_it() -> None:
    proposals = propose_families(
        [
            DocumentHost("a" * 64, "https://www.mckinsey.com/one"),
            DocumentHost("b" * 64, "https://www.mckinsey.com/two"),
            DocumentHost("c" * 64, "https://insights.mckinsey.com/three"),
            DocumentHost("d" * 64, "https://example.org/four"),
        ]
    )
    by_key = {proposal.family_key: proposal for proposal in proposals}
    assert set(by_key) == {"mckinsey-com", "example-org"}
    mckinsey = by_key["mckinsey-com"]
    assert mckinsey.hosts == ("insights.mckinsey.com", "www.mckinsey.com")
    assert len(mckinsey.document_ids) == 3
    # A family of one is still proposed: it is the difference between "this
    # source" and "unknown", and unknown never satisfies a two-source rule.
    assert len(by_key["example-org"].document_ids) == 1


def test_a_batch_that_cannot_be_read_unambiguously_is_refused(tmp_path: Path) -> None:
    cases = [
        ({"families": []}, "who decided"),
        ({"decidedBy": "owner", "families": []}, "no families"),
        ({"decidedBy": "o", "families": [{"action": "confirmed"}]}, "no familyKey"),
        (
            {"decidedBy": "o", "families": [{"familyKey": "x", "action": "maybe"}]},
            "action must be one of",
        ),
        (
            {"decidedBy": "o", "families": [{"familyKey": "x", "action": "confirmed"}]},
            "must say why",
        ),
    ]
    for payload, message in cases:
        path = tmp_path / "batch.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(FamilyBatchError, match=message):
            load_family_batch(path)


def test_one_family_cannot_be_decided_twice_in_one_batch(tmp_path: Path) -> None:
    path = _batch(
        tmp_path / "batch.json",
        [
            {"familyKey": "x", "action": "confirmed", "rationale": "a", "documentIds": ["1"]},
            {"familyKey": "x", "action": "retired", "rationale": "b", "documentIds": []},
        ],
    )
    with pytest.raises(FamilyBatchError, match="appears twice"):
        load_family_batch(path)


# --------------------------------------------------------------------------
# Duplicates
# --------------------------------------------------------------------------


def _text(identifier: str, body: str, digest: str = "") -> DocumentText:
    return DocumentText(
        document_id=identifier,
        canonical_url=f"https://example.com/{identifier}",
        text_sha256=digest or identifier * 8,
        text=body,
    )


def test_an_identical_text_is_a_certain_cluster() -> None:
    proposals = find_hash_clusters(
        [_text("a", BODY, "f" * 64), _text("b", BODY, "f" * 64), _text("c", "other", "e" * 64)]
    )
    assert len(proposals) == 1
    assert proposals[0].document_ids == ("a", "b")
    assert proposals[0].formation_method == "canonical_text_hash"
    assert proposals[0].shingle_threshold is None
    assert all(similarity == 1.0 for _, _, similarity in proposals[0].pairs)


def test_shingle_overlap_records_the_rule_it_was_formed_under() -> None:
    edited = BODY.replace("accountability", "responsibility", 3)
    proposals, stats = find_shingle_clusters(
        [_text("a", BODY), _text("b", edited), _text("c", UNRELATED)],
        threshold=0.7,
    )
    assert len(proposals) == 1
    assert proposals[0].document_ids == ("a", "b")
    assert proposals[0].shingle_threshold == 0.7
    assert proposals[0].shingle_width == 5
    assert stats["compared"] == 3


def test_documents_an_exact_hash_already_explains_are_not_run_through_the_slow_path() -> None:
    # Rediscovering them here would record a certainty under a rule that only
    # claims "probable".
    _, stats = find_shingle_clusters(
        [_text("a", BODY), _text("b", BODY)], exclude=frozenset({"a", "b"})
    )
    assert stats["excluded"] == 2
    assert stats["compared"] == 0
    assert stats["pairs"] == 0


def test_a_text_too_short_to_judge_is_reported_not_clustered() -> None:
    short = "Two short notices about one event."
    proposals, stats = find_shingle_clusters([_text("a", short), _text("b", short)])
    assert proposals == ()
    assert stats["tooShort"] == 2
    assert len(shingles(short)) < MIN_SHINGLES


def test_overlap_is_measured_on_words_not_typography() -> None:
    assert (
        jaccard(
            shingles("The owner is accountable for the outcome of the run"),
            shingles("the OWNER is accountable, for the outcome of the run!"),
        )
        == 1.0
    )


# --------------------------------------------------------------------------
# The counting rules
# --------------------------------------------------------------------------


def test_two_documents_of_one_family_are_one_confirmation(
    migrated_dsn: str, tmp_path: Path
) -> None:
    database = Database(_settings(migrated_dsn))
    first = _store(database, "https://www.mckinsey.com/one", BODY)
    second = _store(database, "https://insights.mckinsey.com/two", BODY + "tail")
    third = _store(database, "https://example.org/three", BODY + "other")
    batch = _batch(
        tmp_path / "batch.json",
        [
            {
                "familyKey": "mckinsey-com",
                "displayName": "McKinsey",
                "action": "confirmed",
                "rationale": "one firm, several hosts",
                "documentIds": [first, second],
            },
            {
                "familyKey": "example-org",
                "action": "confirmed",
                "rationale": "unrelated",
                "documentIds": [third],
            },
        ],
    )
    decided_by, decisions = load_family_batch(batch)
    applied = database.apply_family_batch(decided_by=decided_by, decisions=decisions)
    assert applied["families"] == 2
    assert applied["assignments"] == 3

    report = database.independence_report([first, second, third])
    assert report["documentsConsidered"] == 3
    assert report["independentSources"] == 2
    assert report["collapsedByFamily"] == 1
    assert report["unknownDocuments"] == 0


def test_a_document_with_no_family_is_unknown_and_never_independent(migrated_dsn: str) -> None:
    # ADR-0007 §12: absence of a family is not independence. Fail-closed.
    database = Database(_settings(migrated_dsn))
    document = _store(database, "https://unassigned.example/one", BODY)
    report = database.independence_report([document])
    assert report["documentsConsidered"] == 1
    assert report["independentSources"] == 0
    assert report["unknownDocuments"] == 1


def test_an_unconfirmed_cluster_does_not_collapse_a_count(
    migrated_dsn: str, tmp_path: Path
) -> None:
    # The machine has not been given authority to reduce a count on its own.
    database = Database(_settings(migrated_dsn))
    first = _store(database, "https://outlet-one.example/story", BODY)
    second = _store(database, "https://outlet-two.example/story", BODY + " ")
    batch = _batch(
        tmp_path / "batch.json",
        [
            {
                "familyKey": f"outlet-{index}-example",
                "action": "confirmed",
                "rationale": "separate outlets",
                "documentIds": [document],
            }
            for index, document in enumerate((first, second), start=1)
        ],
    )
    decided_by, decisions = load_family_batch(batch)
    database.apply_family_batch(decided_by=decided_by, decisions=decisions)

    documents = database.documents_for_duplicate_scan(scope="corpus")
    recorded = database.record_duplicate_proposals(
        find_shingle_clusters(documents, threshold=0.5)[0], proposed_by="test"
    )
    assert recorded["clusters"] == 1

    proposed = database.independence_report([first, second])
    assert proposed["independentSources"] == 2
    assert proposed["collapsedByCluster"] == 0

    assert database.confirm_duplicate_clusters(batch_id=recorded["batchId"], confirmed_by="o") == 1
    confirmed = database.independence_report([first, second])
    assert confirmed["independentSources"] == 1
    assert confirmed["collapsedByCluster"] == 1


def test_a_decision_and_an_assignment_cannot_be_rewritten(
    migrated_dsn: str, tmp_path: Path
) -> None:
    database = Database(_settings(migrated_dsn))
    document = _store(database, "https://example.com/one", BODY)
    decided_by, decisions = load_family_batch(
        _batch(
            tmp_path / "batch.json",
            [
                {
                    "familyKey": "example-com",
                    "action": "confirmed",
                    "rationale": "because",
                    "documentIds": [document],
                }
            ],
        )
    )
    database.apply_family_batch(decided_by=decided_by, decisions=decisions)
    for statement in (
        "UPDATE kx.source_family_decisions SET rationale = 'edited'",
        "DELETE FROM kx.document_source_family",
    ):
        with (
            connect(migrated_dsn) as connection,
            connection.cursor() as cursor,
            pytest.raises(Exception, match="immutable|reject"),
        ):
            cursor.execute(statement)


def test_a_correction_is_a_new_decision_and_the_old_one_survives(
    migrated_dsn: str, tmp_path: Path
) -> None:
    database = Database(_settings(migrated_dsn))
    document = _store(database, "https://example.com/one", BODY)
    for index, (key, action) in enumerate(
        [("example-com", "confirmed"), ("newsroom", "corrected")]
    ):
        decided_by, decisions = load_family_batch(
            _batch(
                tmp_path / f"batch-{index}.json",
                [
                    {
                        "familyKey": key,
                        "action": action,
                        "rationale": "first pass" if index == 0 else "same desk as newsroom",
                        "documentIds": [document],
                    }
                ],
            )
        )
        database.apply_family_batch(decided_by=decided_by, decisions=decisions)

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS total FROM kx.source_family_decisions")
        assert cursor.fetchone()["total"] == 2  # type: ignore[index]
        cursor.execute(
            "SELECT family_key FROM kx.document_source_family_current WHERE document_id = %s",
            (document,),
        )
        assert cursor.fetchone()["family_key"] == "newsroom"  # type: ignore[index]
        cursor.execute("SELECT count(*) AS total FROM kx.document_source_family")
        assert cursor.fetchone()["total"] == 2  # type: ignore[index]
