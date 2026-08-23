"""Slice 2.8: what publishes without anybody approving it, and what does not (P19)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import connect
from radar_kx.config import Settings
from radar_kx.database import Database, VersionProvenance
from radar_kx.extraction import Fragment, ProposedClaim, align_all, prompt_sha256
from radar_kx.fetcher import DocumentTask, FetchResult, RawResponse
from radar_kx.identifiers import document_id
from radar_kx.parser import parse_content
from radar_kx.publication import (
    MAX_QUOTE_CHARS,
    build_translation_prompt,
    check_invariants,
    decide,
    latin_names_in,
    normalize_number,
    parse_translation,
    within_one_paragraph,
)
from radar_kx.url_policy import canonical_identity_url

PARAGRAPH_ONE = (
    "Adoption of agentic project management at Deloitte reached 41% in 2026, up "
    "from US$8.5 billion the year before."
)
PARAGRAPH_TWO = (
    "The programme office reported that review time fell from 48 hours to 4 hours "
    "once every run had one named owner."
)
DOCUMENT = f"{PARAGRAPH_ONE}\n\n{PARAGRAPH_TWO}"


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
# Numbers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [("1,000", "1000"), ("1 000", "1000"), ("1000", "1000"), ("3.5", "3.5"), ("3,5", "3.5")],
)
def test_the_same_number_written_two_ways_is_one_number(token: str, expected: str) -> None:
    # A translation is expected to change the separators. What must not change is
    # the value.
    assert normalize_number(token) == expected


def test_every_figure_must_survive_the_translation() -> None:
    good = check_invariants("reached 41% in 2026", "достигло 41% в 2026 году")
    assert not good.blocking
    bad = check_invariants("reached 41% in 2026", "достигло 14% в 2026 году")
    assert bad.blocking
    assert bad.original_numbers == ("2026", "41")
    assert bad.translated_numbers == ("14", "2026")


def test_a_dropped_percent_sign_is_a_different_claim() -> None:
    assert check_invariants("grew 40%", "выросло на 40").blocking


def test_a_currency_written_as_a_word_is_a_correct_translation() -> None:
    # Russian writes "8,5 млрд долл. США" for "US$8.5 billion". The first smoke
    # test of this module blocked exactly that, which is why currency is recorded
    # and not blocking.
    report = check_invariants("US$8.5 billion in 2026", "8,5 млрд долл. США в 2026 году")
    assert not report.blocking
    assert report.currency_symbols["original"] == {"$": 1}
    assert report.currency_symbols["translated"] == {}


def test_the_check_says_what_it_does_not_check() -> None:
    # A check trusted for more than it does is worse than no check.
    report = check_invariants("48 hours", "48 часов")
    assert any("units written as words" in item for item in report.not_checked)
    assert not report.blocking


# --------------------------------------------------------------------------
# Names (P36)
# --------------------------------------------------------------------------


def test_a_sentence_opening_is_not_a_proper_name() -> None:
    assert latin_names_in("Adoption of AI agents at Deloitte rose. Deloitte said so.") == {
        "Deloitte"
    }


def test_a_missing_name_is_a_proposal_and_never_a_block() -> None:
    # P36: an unregistered spelling does not block. The name is shown in the
    # original and the proposal waits with no deadline.
    report = check_invariants(
        "Deloitte reported that Anthropic shipped it.",
        "Deloitte сообщила, что это выпустила компания.",
    )
    assert not report.blocking
    assert report.unresolved_names == ("Anthropic",)


def test_a_registered_alias_resolves_a_name() -> None:
    report = check_invariants(
        "Anthropic published the protocol.",
        "Антропик опубликовала протокол.",
        aliases={"Anthropic": frozenset({"Антропик"})},
    )
    assert report.unresolved_names == ()


# --------------------------------------------------------------------------
# Length (P32)
# --------------------------------------------------------------------------


def test_a_quotation_may_not_cross_a_paragraph() -> None:
    # P32 says "up to a paragraph". Checked against the source's own paragraphs,
    # not against a character count somebody chose.
    assert within_one_paragraph(DOCUMENT, 0, len(PARAGRAPH_ONE))
    assert not within_one_paragraph(DOCUMENT, 0, len(DOCUMENT))


def test_a_source_with_no_paragraph_breaks_still_has_a_backstop() -> None:
    wall = "word " * 500
    assert len(wall) > MAX_QUOTE_CHARS
    assert not within_one_paragraph(wall, 0, len(wall))


# --------------------------------------------------------------------------
# The five conditions
# --------------------------------------------------------------------------


def _decision(**overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "canonical_text": DOCUMENT,
        "char_start": 0,
        "char_end": len(PARAGRAPH_ONE),
        "quote_text": PARAGRAPH_ONE,
        "block_reason": None,
        "caveat": None,
        "invariants": None,
        "independent_sources": 2,
        "independence_required": False,
    }
    arguments.update(overrides)
    return decide(**arguments)


def test_a_clean_quotation_publishes_with_nobody_approving_it() -> None:
    assert _decision().publishable


def test_every_failure_says_what_would_clear_it() -> None:
    # A queue that says only "rejected" is a queue nobody can work.
    cases = {
        "quote_is_not_an_exact_span": _decision(quote_text="something else entirely"),
        "provenance_invalid": _decision(block_reason="provenance_missing"),
        "quote_longer_than_a_paragraph": _decision(char_end=len(DOCUMENT)),
        "source_independence": _decision(independent_sources=1, independence_required=True),
    }
    for condition, decision in cases.items():
        assert not decision.publishable
        entry = next(item for item in decision.quarantine if item.failed_condition == condition)
        assert entry.detail
        assert len(entry.what_would_clear_it) > 20


def test_a_changed_figure_blocks_publication() -> None:
    decision = _decision(invariants=check_invariants("reached 41%", "достигло 14%"))
    assert not decision.publishable
    assert decision.quarantine[0].failed_condition == "invariant_mismatch"


def test_an_archive_without_a_snapshot_publishes_with_a_caveat() -> None:
    # ADR-0004 rule 21a: withheld and caveated are different answers.
    decision = _decision(caveat="text came from a web archive; the snapshot was not preserved")
    assert decision.publishable
    assert decision.caveat is not None


def test_the_translation_prompt_asks_for_the_thing_the_check_enforces() -> None:
    prompt = build_translation_prompt(PARAGRAPH_ONE, target_language="ru")
    assert "Russian" in prompt
    assert "must appear unchanged" in prompt
    assert "data, not instruction" in prompt


def test_a_translation_answer_of_the_wrong_shape_is_refused() -> None:
    assert parse_translation('{"translation": " Внедрение "}') == "Внедрение"
    for answer in ("just prose", '{"text": "x"}'):
        with pytest.raises(ValueError, match="no JSON object|no translation"):
            parse_translation(answer)


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def _claim(database: Database, dsn: str) -> dict[str, Any]:
    url = "https://example.com/report"
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
            (ProposedClaim("reached", "adoption", PARAGRAPH_ONE),),
        ),
        model="glm-5.2",
        prompt_sha256=prompt_sha256(fragment),
    )
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT claim_id, version_id, char_start, char_end, quote_text"
            " FROM kx.claim_evidence LIMIT 1"
        )
        evidence = cursor.fetchone()
        assert evidence is not None
    return dict(evidence)


def test_a_quotation_publishes_automatically_and_says_so(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    _claim(database, migrated_dsn)
    outcome = database.publish_quotes(scope="corpus", target_language="en")
    assert outcome["published"] == 1
    assert outcome["quarantined"] == 0
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT published_automatically, decided_by, attribution, source_url,"
            " quote_chars FROM kx.published_quotes"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["published_automatically"] is True
        assert row["decided_by"] is None
        # P32: attribution and a link, one rule for every kind of source.
        assert row["attribution"]
        assert row["source_url"] == "https://example.com/report"


def test_the_stored_original_must_be_the_span_it_names(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    evidence = _claim(database, migrated_dsn)
    with (
        connect(migrated_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(Exception, match="not the span it names"),
    ):
        cursor.execute(
            "INSERT INTO kx.quote_translations (claim_id, version_id, char_start, char_end,"
            " original_text, source_language, target_language, translated_text, translator,"
            " is_machine, prompt_sha256, invariant_report, created_by)"
            " VALUES (%s, %s, %s, %s, 'not the span', 'en', 'ru', 'x', 'test', false, NULL,"
            " '{}'::jsonb, 'test')",
            (
                evidence["claim_id"],
                evidence["version_id"],
                evidence["char_start"],
                evidence["char_end"],
            ),
        )


def test_a_rejected_translation_and_its_alias_proposals_are_recorded(
    migrated_dsn: str,
) -> None:
    database = Database(_settings(migrated_dsn))
    evidence = _claim(database, migrated_dsn)
    report = check_invariants(PARAGRAPH_ONE, "Внедрение достигло 14% в 2026 году.")
    recorded: dict[str, Any] = database.record_translation(
        claim_id=str(evidence["claim_id"]),
        version_id=str(evidence["version_id"]),
        char_start=int(cast(int, evidence["char_start"])),
        char_end=int(cast(int, evidence["char_end"])),
        original_text=str(evidence["quote_text"]),
        source_language="en",
        target_language="ru",
        translated_text="Внедрение достигло 14% в 2026 году.",
        translator="glm-5.2",
        is_machine=True,
        prompt_sha256="a" * 64,
        report=report,
        created_by="test",
    )
    assert recorded["state"] == "rejected"
    assert recorded["aliasProposals"] >= 1
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT original_form, decided_at FROM kx.entity_alias_proposals")
        rows = [dict(row) for row in cursor.fetchall()]
        assert rows
        assert all(row["decided_at"] is None for row in rows)


def test_a_published_quotation_cannot_be_rewritten(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    _claim(database, migrated_dsn)
    database.publish_quotes(scope="corpus", target_language="en")
    with (
        connect(migrated_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(Exception, match="immutable|reject"),
    ):
        cursor.execute("UPDATE kx.published_quotes SET original_text = 'edited'")


def test_the_report_separates_published_from_quarantined(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    _claim(database, migrated_dsn)
    database.publish_quotes(scope="corpus", target_language="en")
    report: dict[str, Any] = database.publication_report()
    assert report["published"]["total"] == 1
    assert report["published"]["automatic"] == 1
    assert report["quarantineByCondition"] == {}
    assert report["openAliasProposals"] == 0


def test_a_quotation_awaiting_translation_is_skipped_and_not_quarantined(
    migrated_dsn: str,
) -> None:
    # Quarantine is for an item that failed a condition. Work not yet done is not
    # a failure, and mixing the two makes the queue unreadable.
    database = Database(_settings(migrated_dsn))
    _claim(database, migrated_dsn)
    outcome = database.publish_quotes(scope="corpus", target_language="ru")
    assert outcome["published"] == 0
    assert outcome["quarantined"] == 0
    assert outcome["awaitingTranslation"] == 1


def _fetched(database: Database, dsn: str, url: str) -> str:
    """A version obtained the way the worker obtains one: no provenance row.

    This is the state 6 464 production versions were in. It cannot be reached by
    storing a version and then deleting its provenance, because version_provenance
    is immutable - which is the trigger doing its job.
    """
    parsed = parse_content(
        body=DOCUMENT.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        source_url=url,
        min_text_chars=50,
    )
    result = FetchResult(
        task=DocumentTask(
            document_id=document_id(canonical_identity_url(url)),
            canonical_url=url,
            attempt_count=1,
            etag=None,
            last_modified=None,
        ),
        response=RawResponse(
            requested_url=url,
            final_url=url,
            started_at=datetime(2026, 8, 23, tzinfo=UTC),
            fetched_at=datetime(2026, 8, 23, tzinfo=UTC),
            http_status=200,
            content_type="text/plain; charset=utf-8",
            headers={},
            body=DOCUMENT.encode("utf-8"),
        ),
        parsed=parsed,
        error_code=None,
        error_detail=None,
        retryable=False,
        not_modified=False,
    )
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.documents (document_id, canonical_url) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING",
            (result.task.document_id, url),
        )
        cursor.execute(
            "INSERT INTO kx.fetch_queue (document_id, status) VALUES (%s, 'running')"
            " ON CONFLICT DO NOTHING",
            (result.task.document_id,),
        )
    database.record_fetch_result(result)
    return str(result.task.document_id)


def test_provenance_is_restated_from_the_fetch_that_produced_the_bytes(
    migrated_dsn: str,
) -> None:
    # Migration 003 made provenance a precondition of publication and only 25
    # documents ever had it, so publication withheld 6 464 versions the worker had
    # fetched itself: the first automatic run published 46 and quarantined 298 for
    # provenance_invalid. The attempt row already says how they were obtained.
    database = Database(_settings(migrated_dsn))
    _fetched(database, migrated_dsn, "https://fetched.example/report")

    outcome: dict[str, Any] = database.backfill_provenance_from_fetches()
    assert outcome["recorded"] == {"http_default": 1}

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT source_access_method, manual_review_required, archive_used, notes"
            " FROM kx.version_provenance"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["source_access_method"] == "http_default"
        assert row["manual_review_required"] is False
        assert row["archive_used"] is False
        # It says where it came from, so nobody later reads it as a hand-entered fact.
        assert "restated from the fetch attempt" in str(row["notes"])


def test_the_backfill_never_invents_provenance_for_a_version_with_no_fetch(
    migrated_dsn: str,
) -> None:
    # A locally imported version has no fetch attempt and there is nothing to
    # restate. Publication keeps withholding it, which is correct.
    database = Database(_settings(migrated_dsn))
    _claim(database, migrated_dsn)
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS total FROM kx.version_provenance")
        before = cursor.fetchone()
        assert before is not None
    outcome: dict[str, Any] = database.backfill_provenance_from_fetches()
    assert outcome["considered"] == 0
    assert outcome["recorded"] == {}


def test_a_backfilled_version_then_publishes(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    document = _fetched(database, migrated_dsn, "https://fetched.example/report")
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT version_id FROM kx.document_versions WHERE document_id = %s",
            (document,),
        )
        version = cursor.fetchone()
        assert version is not None
        cursor.execute(
            "SELECT chunk_id, char_start, char_end, text FROM kx.chunks"
            " WHERE version_id = %s ORDER BY ordinal LIMIT 1",
            (version["version_id"],),
        )
        chunk = cursor.fetchone()
        assert chunk is not None
    fragment = Fragment(
        version_id=str(version["version_id"]),
        chunk_id=str(chunk["chunk_id"]),
        char_start=int(cast(int, chunk["char_start"])),
        char_end=int(cast(int, chunk["char_end"])),
        text=str(chunk["text"]),
    )
    database.record_extraction(
        fragment,
        align_all(
            fragment,
            database.canonical_text(str(version["version_id"])),
            (ProposedClaim("reached", "adoption", PARAGRAPH_ONE),),
        ),
        model="glm-5.2",
        prompt_sha256=prompt_sha256(fragment),
    )
    assert database.publish_quotes(scope="corpus", target_language="en")["published"] == 0
    database.backfill_provenance_from_fetches()
    assert database.publish_quotes(scope="corpus", target_language="en")["published"] == 1


def test_the_backfill_reads_the_outcome_the_fetcher_actually_writes() -> None:
    # The first version filtered on outcome = 'stored', which the fetcher never
    # writes, so the backfill silently found nothing. The vocabulary is the
    # fetcher's: succeeded, robots_denied, weak_or_missing_text, http_403, ...
    source = (Path(__file__).parents[1] / "src" / "radar_kx" / "database.py").read_text(
        encoding="utf-8"
    )
    assert "attempt.outcome = 'succeeded'" in source
    assert "attempt.outcome = 'stored'" not in source
