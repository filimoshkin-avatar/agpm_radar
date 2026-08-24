"""Stage 0b: what one reading of a statement decides, and what it refuses to guess.

The prompt asks for five things at once, so the parser is where the damage would
be. Every case here is a way an answer can be wrong that would otherwise become a
row nobody could tell from a good one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from radar_kx.reading import (
    ADMISSIONS,
    MATERIAL_KINDS,
    MAX_TOPICS,
    QUOTE_CHARS,
    ReadableClaim,
    ReadingError,
    build_instructions,
    build_payload,
    parse_readings,
    summarize,
    valid_until,
)

TOPICS = [
    {
        "topic_key": "avtonomiya-porogi",
        "title": "Пороги автономии",
        "path": "Управление / Автономия",
    },
    {"topic_key": "riski-kontrol", "title": "Риски и контроль", "path": "Управление / Риски"},
]
ALLOWED = frozenset(topic["topic_key"] for topic in TOPICS)


def claim(claim_id: str = "c1", *, corpus: str = "материал выпуска") -> ReadableClaim:
    return ReadableClaim(
        claim_id=claim_id,
        statement="порог автономии определяет, до какой суммы агент действует сам",
        quote="Порог автономии определяет границу между классами решений.",
        corpus=corpus,
        dated_on=datetime(2026, 6, 1, tzinfo=UTC),
    )


def answer(**overrides: object) -> str:
    row = {
        "item": 1,
        "kind": "fact",
        "source": "",
        "retelling": False,
        "admission": "knowledge",
        "note": None,
        "topics": ["avtonomiya-porogi"],
        "missing": None,
        "confidence": 0.8,
    }
    row.update(overrides)
    return json.dumps([row], ensure_ascii=False)


# ---------------------------------------------------------------------------
# What the prompt carries
# ---------------------------------------------------------------------------


def test_the_instructions_carry_the_backbone_and_every_allowed_value() -> None:
    text = build_instructions(TOPICS)
    for kind in MATERIAL_KINDS:
        assert kind in text
    for admission in ADMISSIONS:
        assert admission in text
    assert "avtonomiya-porogi" in text
    assert "Пороги автономии" in text


def test_the_payload_carries_the_corpus_and_a_capped_quotation() -> None:
    long_quote = ReadableClaim(
        claim_id="c1",
        statement="утверждение",
        quote="я" * (QUOTE_CHARS * 2),
        corpus="канон AgPM",
        dated_on=None,
    )
    payload = build_payload([long_quote])
    assert "[канон AgPM]" in payload
    assert payload.count("я") == QUOTE_CHARS


# ---------------------------------------------------------------------------
# Reading the answer back
# ---------------------------------------------------------------------------


def test_a_clean_answer_becomes_one_reading() -> None:
    readings, dropped = parse_readings(answer(), [claim()], ALLOWED)
    assert len(readings) == 1
    assert readings[0].material_kind == "fact"
    assert readings[0].admission == "knowledge"
    assert readings[0].topic_keys == ("avtonomiya-porogi",)
    assert readings[0].confidence == 0.8
    assert not dropped["unknownItem"]


def test_a_kind_the_owner_did_not_name_drops_the_row() -> None:
    readings, dropped = parse_readings(answer(kind="rumour"), [claim()], ALLOWED)
    assert readings == ()
    assert dropped["unknownKind"] == 1


def test_an_admission_the_owner_did_not_name_drops_the_row() -> None:
    readings, dropped = parse_readings(answer(admission="maybe"), [claim()], ALLOWED)
    assert readings == ()
    assert dropped["unknownAdmission"] == 1


def test_an_invented_subject_is_dropped_and_the_rest_of_the_reading_survives() -> None:
    readings, dropped = parse_readings(
        answer(topics=["avtonomiya-porogi", "тема-которой-нет"]), [claim()], ALLOWED
    )
    assert readings[0].topic_keys == ("avtonomiya-porogi",)
    assert dropped["unknownTopic"] == 1


def test_more_subjects_than_the_cap_are_cut_to_it() -> None:
    readings, _ = parse_readings(
        answer(topics=["avtonomiya-porogi", "riski-kontrol", "avtonomiya-porogi"]),
        [claim()],
        ALLOWED,
    )
    assert len(readings[0].topic_keys) <= MAX_TOPICS
    # A repeated key is one subject, not two.
    assert readings[0].topic_keys == ("avtonomiya-porogi", "riski-kontrol")


def test_a_retelling_that_cannot_name_its_source_is_not_a_retelling() -> None:
    """The table's own constraint refuses it; downgrading keeps the other four answers."""
    readings, _ = parse_readings(answer(retelling=True, source=""), [claim()], ALLOWED)
    assert readings[0].is_retelling is False
    assert readings[0].primary_source == ""


def test_a_named_retelling_keeps_both_halves() -> None:
    readings, _ = parse_readings(answer(retelling=True, source="Gartner"), [claim()], ALLOWED)
    assert readings[0].is_retelling is True
    assert readings[0].primary_source == "Gartner"


def test_a_statement_with_no_place_on_the_backbone_records_what_was_missing() -> None:
    readings, _ = parse_readings(
        answer(topics=[], missing="нет темы про страхование агентных решений"),
        [claim()],
        ALLOWED,
    )
    assert readings[0].topic_keys == ()
    assert readings[0].missing is not None
    assert "страхование" in readings[0].missing


def test_a_missing_note_beside_a_subject_is_dropped() -> None:
    """A gap and a placement are mutually exclusive answers, and the gap map is a queue."""
    readings, _ = parse_readings(answer(missing="что-то"), [claim()], ALLOWED)
    assert readings[0].topic_keys
    assert readings[0].missing is None


def test_an_item_number_nobody_asked_about_is_dropped() -> None:
    readings, dropped = parse_readings(answer(item=7), [claim()], ALLOWED)
    assert readings == ()
    assert dropped["unknownItem"] >= 1


def test_a_claim_the_answer_skipped_is_counted() -> None:
    readings, dropped = parse_readings(answer(), [claim("c1"), claim("c2")], ALLOWED)
    assert len(readings) == 1
    assert dropped["unknownItem"] == 1


def test_the_same_item_answered_twice_counts_once() -> None:
    doubled = json.dumps(json.loads(answer()) * 2, ensure_ascii=False)
    readings, dropped = parse_readings(doubled, [claim()], ALLOWED)
    assert len(readings) == 1
    assert dropped["unknownItem"] == 1


def test_a_fenced_answer_is_still_read() -> None:
    readings, _ = parse_readings(f"```json\n{answer()}\n```", [claim()], ALLOWED)
    assert len(readings) == 1


def test_an_answer_with_no_array_is_an_error_not_an_empty_result() -> None:
    with pytest.raises(ReadingError):
        parse_readings("не могу", [claim()], ALLOWED)


@pytest.mark.parametrize(
    ("given", "expected"), [(1.4, 1.0), (-2, 0.0), ("0.55", 0.55), (None, None), ([], None)]
)
def test_confidence_is_kept_inside_its_range(given: object, expected: float | None) -> None:
    readings, _ = parse_readings(answer(confidence=given), [claim()], ALLOWED)
    assert readings[0].confidence == expected


# ---------------------------------------------------------------------------
# The expiry, which is arithmetic rather than a judgement
# ---------------------------------------------------------------------------


def test_the_expiry_is_counted_from_the_publication_date() -> None:
    rules = {"forecast": timedelta(days=365), "case": None}
    published = datetime(2026, 6, 1, tzinfo=UTC)
    assert valid_until("forecast", published, rules) == published + timedelta(days=365)


def test_a_kind_with_no_interval_never_expires() -> None:
    assert valid_until("case", datetime(2026, 6, 1, tzinfo=UTC), {"case": None}) is None
    assert valid_until("fact", None, {}) is None


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_the_summary_separates_what_was_read_from_what_was_thrown_away() -> None:
    readings, dropped = parse_readings(
        json.dumps(
            [
                json.loads(answer())[0],
                json.loads(answer(item=2, kind="forecast", admission="observatory", topics=[]))[0],
            ],
            ensure_ascii=False,
        ),
        [claim("c1"), claim("c2")],
        ALLOWED,
    )
    report = summarize(readings, dropped)
    assert report["read"] == 2
    assert report["byMaterialKind"] == {"fact": 1, "forecast": 1}
    assert report["byAdmission"] == {"knowledge": 1, "observatory": 1}
    assert report["withoutASubject"] == 1
    # Nothing was thrown away, so the report does not carry an empty tally.
    assert report["dropped"] == {}


def test_a_statement_nobody_can_date_has_no_expiry() -> None:
    """The anchor is the document, never the clock.

    Falling back to `now()` put 6 625 of 13 876 production statements on a clock
    that started the day the reading pass ran - months after the material - and
    made them all expire together on its anniversary. A date nobody knows is
    `None`, which keeps the statement out of the expiry queue instead of giving
    it a number that is really a record of when a job ran.
    """
    from radar_kx.reading import valid_until

    rules = {"forecast": timedelta(days=365)}
    assert valid_until("forecast", None, rules) is None
    dated = datetime(2025, 11, 2, tzinfo=UTC)
    assert valid_until("forecast", dated, rules) == datetime(2026, 11, 2, tzinfo=UTC)
    assert valid_until("fact", dated, {"fact": None}) is None


def test_an_invented_subject_is_not_reported_as_no_subject() -> None:
    """Two different gaps, and the owner acts on them differently.

    "Nothing was named" is a statement with no subject. "These were named and the
    backbone has none of them" is a candidate subject list - so the names go into
    the line rather than being replaced by the other message.
    """
    from radar_kx.reading import NO_SUBJECT_NAMED, not_in_the_backbone

    assert not_in_the_backbone(()) == NO_SUBJECT_NAMED
    line = not_in_the_backbone(("agent/orchestration", "agent/handoff"))
    assert "agent/orchestration" in line
    assert "agent/handoff" in line
    assert line != NO_SUBJECT_NAMED


def test_a_source_that_is_not_a_name_is_not_stored_as_one() -> None:
    """`str(False)` is "False", which reads as a name and satisfies the CHECK.

    `a_retelling_names_its_source` only asks that the field be non-empty, so a
    model answering `"source": false` would have produced a retelling attributed
    to somebody called False. None reached production; the check is here so none
    can.
    """
    readings, _ = parse_readings(
        json.dumps(
            [
                {
                    "item": 1,
                    "kind": "fact",
                    "source": False,
                    "retelling": False,
                    "admission": "knowledge",
                    "topics": [],
                }
            ]
        ),
        (
            ReadableClaim(
                claim_id="11111111-1111-1111-1111-111111111111",
                statement="что-то",
                quote="что-то",
                corpus="материал выпуска",
                dated_on=None,
            ),
        ),
        frozenset(),
    )
    assert readings[0].primary_source == ""
    assert readings[0].is_retelling is False
