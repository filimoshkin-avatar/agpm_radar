"""The entity pass: what a statement names, and what is refused.

`entities` has been in the schema since migration 001 and empty since. Of UC-05's
six graph modes, that leaves four with nothing to draw.
"""

from __future__ import annotations

import json

import pytest

from radar_kx.entities import (
    ENTITY_TYPES,
    MAX_ENTITIES,
    EntityError,
    build_payload,
    parse_mentions,
    summarize,
)

CLAIMS = [
    {"claim_id": "c1", "quote_text": "Gartner прогнозирует рост автономии агентов."},
    {"claim_id": "c2", "quote_text": "Ничего конкретного здесь не названо."},
]


def answer(rows: list[dict[str, object]]) -> str:
    return json.dumps(rows, ensure_ascii=False)


def test_the_type_list_is_closed() -> None:
    """A graph mode is only a mode if "organisation" means one thing."""
    found, dropped = parse_mentions(
        answer([{"item": 1, "entities": [{"type": "консалтинг", "name": "Gartner"}]}]), CLAIMS
    )
    assert found == ()
    assert dropped["unknownType"] == 1
    assert len(ENTITY_TYPES) == 9


def test_a_general_word_is_not_a_name() -> None:
    """The reading pass learned this about `primary_source` and paid for it.

    A model asked for an organisation will answer "аналитики" unless told that
    is not one - and "аналитики" as a node would collect every statement that
    cites anybody.
    """
    found, dropped = parse_mentions(
        answer(
            [
                {
                    "item": 1,
                    "entities": [
                        {"type": "organisation", "name": "аналитики"},
                        {"type": "organisation", "name": "Gartner", "role": "subject"},
                    ],
                }
            ]
        ),
        CLAIMS,
    )
    assert dropped["emptyName"] == 1
    assert [mention.canonical_name for mention in found] == ["Gartner"]
    assert found[0].role == "subject"


def test_naming_nothing_is_an_answer() -> None:
    """Most statements name nothing, and that must not read as a failed batch."""
    found, dropped = parse_mentions(
        answer([{"item": 1, "entities": []}, {"item": 2, "entities": []}]), CLAIMS
    )
    assert found == ()
    assert not dropped


def test_the_same_entity_twice_in_one_statement_is_one_row() -> None:
    found, dropped = parse_mentions(
        answer(
            [
                {
                    "item": 1,
                    "entities": [
                        {"type": "organisation", "name": "Gartner", "form": "Гартнер"},
                        {"type": "organisation", "name": "gartner"},
                    ],
                }
            ]
        ),
        CLAIMS,
    )
    assert len(found) == 1
    assert dropped["duplicate"] == 1
    # The surface form is kept: it is how a source actually wrote the name.
    assert found[0].surface_form == "Гартнер"


def test_a_list_of_things_is_capped() -> None:
    """A sentence naming more than six is a list, and a list is `rejected`."""
    many = [{"type": "platform", "name": f"Продукт {n}"} for n in range(20)]
    found, _ = parse_mentions(answer([{"item": 1, "entities": many}]), CLAIMS)
    assert len(found) == MAX_ENTITIES


def test_an_unknown_role_falls_back_rather_than_dropping_the_entity() -> None:
    """The type is the claim; the role is a nuance and must not lose the name."""
    found, _ = parse_mentions(
        answer([{"item": 1, "entities": [{"type": "person", "name": "Ada", "role": "главный"}]}]),
        CLAIMS,
    )
    assert found[0].role == "mentioned"


def test_an_answer_about_a_statement_that_was_not_sent_is_refused() -> None:
    found, dropped = parse_mentions(
        answer([{"item": 9, "entities": [{"type": "person", "name": "Ada"}]}]), CLAIMS
    )
    assert found == ()
    assert dropped["unknownItem"] == 1


def test_an_answer_that_is_not_json_says_so() -> None:
    with pytest.raises(EntityError):
        parse_mentions("совершенно не JSON", CLAIMS)


def test_the_payload_carries_the_quotation_and_nothing_else() -> None:
    """Never the document: what a sentence names is in the sentence."""
    payload = build_payload(CLAIMS)
    assert "Gartner прогнозирует" in payload
    assert "c1" not in payload, "an identifier is not evidence and costs tokens"


def test_the_summary_separates_what_was_found_from_what_was_thrown_away() -> None:
    found, dropped = parse_mentions(
        answer(
            [
                {
                    "item": 1,
                    "entities": [
                        {"type": "organisation", "name": "Gartner"},
                        {"type": "лишний", "name": "X"},
                    ],
                }
            ]
        ),
        CLAIMS,
    )
    counted = summarize(found, dropped)
    assert counted["mentions"] == 1
    assert counted["byType"] == {"organisation": 1}
    assert counted["dropped"]["unknownType"] == 1
