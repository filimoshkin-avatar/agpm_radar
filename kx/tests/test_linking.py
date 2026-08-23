"""Stage 2: what one statement does to another, and what the judge may not invent.

The whole value of this layer is the difference between "confirms" and
"contradicts" - two sentences that are maximally similar by construction. So the
tests are about the parser refusing everything that is not one of the five
answers, and about `none` being an answer rather than a failure.
"""

from __future__ import annotations

import json

import pytest

from radar_kx.linking import (
    BATCH,
    INSTRUCTIONS,
    LINK_TYPES,
    MAX_DISTANCE,
    NEIGHBOURS,
    STATEMENT_CHARS,
    Judgement,
    LinkingError,
    Pair,
    build_payload,
    parse_judgements,
    summarize,
)


def pair(from_id: str = "a", to_id: str = "b") -> Pair:
    return Pair(
        from_id=from_id,
        to_id=to_id,
        from_text="порог автономии определяет границу классов решений",
        to_text="без количественного порога классы решений остаются абстракцией",
        distance=0.21,
        shared_topic="Пороги автономии",
    )


def answer(link: str, item: int = 1) -> str:
    return json.dumps([{"item": item, "link": link}], ensure_ascii=False)


def test_the_instructions_name_all_four_types_and_the_fifth_answer() -> None:
    for link_type in LINK_TYPES:
        assert link_type in INSTRUCTIONS
    assert "none" in INSTRUCTIONS


def test_the_payload_carries_two_statements_and_nothing_else() -> None:
    text = build_payload([pair()])
    assert "порог автономии" in text
    assert "абстракцией" in text
    # No quotation, no url, no document - the egress rule for this run type.
    assert "http" not in text


def test_a_long_statement_is_cut_rather_than_sent_whole() -> None:
    long_pair = Pair(
        from_id="a",
        to_id="b",
        from_text="я" * (STATEMENT_CHARS * 2),
        to_text="б",
        distance=0.1,
        shared_topic="т",
    )
    assert build_payload([long_pair]).count("я") == STATEMENT_CHARS


@pytest.mark.parametrize("link_type", LINK_TYPES)
def test_each_of_the_four_types_is_recorded(link_type: str) -> None:
    judged, dropped = parse_judgements(answer(link_type), [pair()])
    assert len(judged) == 1
    assert judged[0].link_type == link_type
    assert not dropped["unknownLink"]


def test_none_is_an_answer_not_a_failure() -> None:
    """The expected answer for most pairs: the shortlist is wide on purpose."""
    judged, dropped = parse_judgements(answer("none"), [pair()])
    assert judged == ()
    assert dropped == {"unknownItem": 0, "unknownLink": 0}


def test_a_relation_the_owner_did_not_keep_is_dropped() -> None:
    """Fourteen of her eighteen types wait in her document, not in the store."""
    judged, dropped = parse_judgements(answer("broader_than"), [pair()])
    assert judged == ()
    assert dropped["unknownLink"] == 1


def test_a_pair_number_nobody_offered_is_dropped() -> None:
    judged, dropped = parse_judgements(answer("supports", item=9), [pair()])
    assert judged == ()
    assert dropped["unknownItem"] == 1


def test_the_same_pair_judged_twice_counts_once() -> None:
    doubled = json.dumps(json.loads(answer("supports")) * 2, ensure_ascii=False)
    judged, dropped = parse_judgements(doubled, [pair()])
    assert len(judged) == 1
    assert dropped["unknownItem"] == 1


def test_an_answer_with_no_array_is_an_error() -> None:
    with pytest.raises(LinkingError):
        parse_judgements("не могу судить", [pair()])


def test_a_fenced_answer_is_still_read() -> None:
    judged, _ = parse_judgements(f"```json\n{answer('qualifies')}\n```", [pair()])
    assert judged[0].link_type == "qualifies"


def test_the_summary_counts_what_was_left_alone() -> None:
    report = summarize(
        [Judgement("a", "b", "supports"), Judgement("c", "d", "contradicts")],
        pairs_judged=10,
        dropped={"unknownItem": 0, "unknownLink": 2},
    )
    assert report["pairsJudged"] == 10
    assert report["linked"] == 2
    assert report["leftUnlinked"] == 8
    assert report["byLinkType"]["supports"] == 1
    assert report["dropped"] == {"unknownLink": 2}


def test_the_shortlist_constants_stay_inside_what_a_call_can_hold() -> None:
    """Twenty pairs at 300 characters each has to fit the run type's cap."""
    from radar_kx.orchestrator import KNOWLEDGE_LINK

    worst_case = BATCH * (STATEMENT_CHARS * 2 + 40) + len(INSTRUCTIONS)
    assert worst_case <= KNOWLEDGE_LINK.max_payload_chars
    assert 0 < MAX_DISTANCE < 1
    assert NEIGHBOURS >= 1
