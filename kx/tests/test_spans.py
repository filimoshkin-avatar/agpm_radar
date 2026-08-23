"""Stage 0a: what the boundary repair moves, and everything it refuses to move.

Every case here is a shape that actually occurs in the store, written out small
enough to read. The counts beside them are the production measurement of
2026-08-23 over 13 876 spans.
"""

from __future__ import annotations

import pytest

from radar_kx.publication import MAX_QUOTE_CHARS as PUBLICATION_CAP
from radar_kx.publication import within_one_paragraph
from radar_kx.spans import (
    EXPANDED_TO_BLOCK,
    EXPANDED_TO_SENTENCE,
    KEPT_OUT_OF_REACH,
    KEPT_STRUCTURAL,
    MAX_QUOTE_CHARS,
    MAX_SIDE_GROWTH,
    UNCHANGED_BOUNDARY,
    Expansion,
    Repair,
    block_of,
    expand,
    is_terminator,
    summarize,
)


def widen(text: str, quote: str) -> tuple[str, Expansion]:
    """Expand the span that holds `quote`, and hand back what it became."""
    start = text.index(quote)
    result = expand(text, start, start + len(quote))
    return text[result.start : result.end], result


# ---------------------------------------------------------------------------
# The repair itself
# ---------------------------------------------------------------------------


def test_a_span_cut_before_its_full_stop_gets_it_back() -> None:
    """1 053 spans in production end one character short of the full stop."""
    text = "Контекст. Лишь небольшая доля задач делегируется агенту полностью. Дальше."
    quoted, result = widen(text, "Лишь небольшая доля задач делегируется агенту полностью")
    assert quoted == "Лишь небольшая доля задач делегируется агенту полностью."
    assert result.left_reason == UNCHANGED_BOUNDARY
    assert result.right_reason == EXPANDED_TO_SENTENCE


def test_a_span_starting_after_a_comma_recovers_what_the_sentence_said_first() -> None:
    """The lead-in carries the date and the scope; without it the figure floats."""
    text = (
        "Nothing before. By February 2026, five of the six US military branches"
        " had formally adopted GenAI.mil. Nothing after."
    )
    quoted, result = widen(text, "five of the six US military branches had formally adopted")
    assert quoted.startswith("By February 2026,")
    assert quoted.endswith("adopted GenAI.mil.")
    assert result.left_reason == EXPANDED_TO_SENTENCE


def test_both_sides_move_when_both_are_torn() -> None:
    text = "One sentence. A second one that is quoted from its middle. A third."
    quoted, result = widen(text, "that is quoted from its")
    assert quoted == "A second one that is quoted from its middle."
    assert result.left_reason == EXPANDED_TO_SENTENCE
    assert result.right_reason == EXPANDED_TO_SENTENCE


def test_the_original_span_is_always_inside_the_widened_one() -> None:
    text = "Alpha beta. Gamma delta epsilon zeta. Eta theta."
    quote = "delta epsilon"
    quoted, _ = widen(text, quote)
    assert quote in quoted


# ---------------------------------------------------------------------------
# What is already on a boundary is not touched
# ---------------------------------------------------------------------------


def test_a_whole_sentence_is_left_exactly_as_it_is() -> None:
    text = "Before it. A complete sentence stands alone. After it."
    quoted, result = widen(text, "A complete sentence stands alone.")
    assert quoted == "A complete sentence stands alone."
    assert result.left_reason == UNCHANGED_BOUNDARY
    assert result.right_reason == UNCHANGED_BOUNDARY


def test_a_list_item_starts_where_its_marker_ends() -> None:
    """678 spans start right after a bullet. That is the item's own beginning."""
    text = "Intro line.\n- 78% of leaders hit data readiness issues\n- Another item.\n"
    quoted, result = widen(text, "78% of leaders hit data readiness issues")
    assert quoted == "78% of leaders hit data readiness issues"
    assert result.block_kind == "list item"
    assert result.left_reason == UNCHANGED_BOUNDARY


def test_a_heading_is_a_heading() -> None:
    text = "## Agentic governance in 2026\n\nBody text follows here.\n"
    quoted, result = widen(text, "Agentic governance in 2026")
    assert quoted == "Agentic governance in 2026"
    assert result.block_kind == "heading"
    assert result.left_reason == KEPT_STRUCTURAL


def test_a_table_cell_does_not_swallow_its_neighbours() -> None:
    """294 spans begin just after a pipe. Widening across one would fuse columns."""
    text = "| Vendor | Capability | Score |\n| Glean | Enterprise search | 7.2 |\n"
    quoted, result = widen(text, "Enterprise search")
    assert quoted == "Enterprise search"
    assert result.block_kind == "table cell"
    assert result.right_reason == KEPT_STRUCTURAL


def test_a_row_without_a_leading_pipe_is_still_a_row() -> None:
    """Half this corpus's tables arrived with the pipes only between the columns.

    Read as prose, the row label is one sentence away from the cell beside it, and
    widening fused the two - 41 quotations before the row was recognised.
    """
    text = (
        "Порог автономии | Граница между классами. | Без порога классы абстрактны.\n"
        "Значимое решение | Рекомендательный класс и выше. | Разводит два случая.\n"
    )
    quoted, result = widen(text, "Рекомендательный класс и выше.")
    assert quoted == "Рекомендательный класс и выше."
    assert result.block_kind == "table cell"
    assert "Значимое решение" not in quoted


# ---------------------------------------------------------------------------
# Where widening stops
# ---------------------------------------------------------------------------


def test_widening_never_crosses_a_paragraph_break() -> None:
    text = "First paragraph ends here.\n\nsecond paragraph starts lowercase and runs on.\n"
    quoted, _ = widen(text, "second paragraph starts")
    assert "First paragraph" not in quoted
    assert within_one_paragraph(text, text.index(quoted), text.index(quoted) + len(quoted))


def test_a_wrapped_line_is_crossed_and_a_row_is_not() -> None:
    """Half this corpus is hard-wrapped PDF text; the other half is flattened tables."""
    wrapped = (
        "Параллелизм достигается через делегирование подзадач субагентам с изоляцией\n"
        "контекста, а не через многопоточность внутри одного агента.\n"
    )
    quoted, _ = widen(wrapped, "через делегирование подзадач субагентам с изоляцией")
    assert quoted.endswith("одного агента.")

    table = (
        "Lab / Provider Basic tier $/mo Most expensive tier $/mo\n"
        "OpenAI ChatGPT Plus $20 Pro $200\n"
        "Google DeepMind Gemini AI Pro $20 AI Ultra $250\n"
    )
    quoted, result = widen(table, "ChatGPT Plus $20 Pro")
    assert "Google DeepMind" not in quoted
    assert result.right_reason == EXPANDED_TO_BLOCK


def test_a_side_that_would_travel_further_than_a_sentence_stays_put() -> None:
    """A glued catalogue has no sentence to reach; the parse defect is not this one's."""
    entry = "ГОСТ Р 59988 Соединители электрические изделия "
    text = "Перечень " + entry * 24 + "конец. Хвостовое предложение.\n"
    start = text.index(entry, MAX_SIDE_GROWTH + len(entry))
    result = expand(text, start, start + len(entry))
    assert result.left_reason == KEPT_OUT_OF_REACH
    assert result.start == start


def test_the_result_never_exceeds_the_publication_cap() -> None:
    assert MAX_QUOTE_CHARS == PUBLICATION_CAP
    sentence = "Слово " * 400 + "конец."
    text = f"Начало. {sentence}\n"
    start = text.index("конец")
    result = expand(text, start, start + 5)
    assert result.end - result.start <= MAX_QUOTE_CHARS


def test_growth_is_bounded_on_each_side() -> None:
    filler = "и слово " * 200
    text = f"Начало предложения {filler}середина {filler}конец предложения."
    start = text.index("середина")
    result = expand(text, start, start + len("середина"))
    assert start - result.start <= MAX_SIDE_GROWTH
    assert result.end - (start + len("середина")) <= MAX_SIDE_GROWTH


# ---------------------------------------------------------------------------
# Reading a full stop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "index", "expected"),
    [
        ("Ends here. Next", 9, True),
        ("Version 3.5 of it", 9, False),
        ("The U.S. Army said", 7, False),
        ("см. рисунок 2", 2, False),
        ("т.е. дальше", 3, False),
        ("Ends here.", 9, True),
        ("воздействий.Примечание", 11, True),
        ("example.com pages", 7, False),
    ],
)
def test_a_full_stop_is_read_in_its_neighbourhood(text: str, index: int, expected: bool) -> None:
    assert is_terminator(text, index) is expected


def test_a_question_mark_ends_a_sentence() -> None:
    text = "Before. Нужна ли оценка воздействия? После."
    quoted, _ = widen(text, "оценка воздействия")
    assert quoted == "Нужна ли оценка воздействия?"


# ---------------------------------------------------------------------------
# The block, and the report
# ---------------------------------------------------------------------------


def test_the_block_knows_its_widest_line() -> None:
    text = "short\na much longer line than the first\nmid line\n"
    block = block_of(text, text.index("mid"), text.index("mid") + 3)
    assert block.kind == "paragraph"
    assert block.wrap_width == len("a much longer line than the first")


def test_the_summary_counts_by_the_reason_each_side_gave() -> None:
    def repair(quote: str, widened: str, left: str, right: str) -> Repair:
        return Repair(
            claim_id="c",
            version_id="v",
            old_start=0,
            old_end=len(quote),
            quote=quote,
            widened=widened,
            expansion=Expansion(0, len(widened), "paragraph", left, right),
        )

    report = summarize(
        [
            repair("a", "a beta.", UNCHANGED_BOUNDARY, EXPANDED_TO_SENTENCE),
            repair("b", "b", UNCHANGED_BOUNDARY, UNCHANGED_BOUNDARY),
        ]
    )
    assert report["examined"] == 2
    assert report["changed"] == 1
    assert report["unchanged"] == 1
    assert report["byRightBoundary"][EXPANDED_TO_SENTENCE] == 1
    assert report["charactersAdded"]["max"] == 6


def test_an_example_shows_what_each_side_gained() -> None:
    text = "Первое. Второе предложение целиком. Третье."
    start = text.index("Второе предложение")
    result = expand(text, start + 7, start + 18)
    repair = Repair(
        claim_id="c",
        version_id="v",
        old_start=start + 7,
        old_end=start + 18,
        quote=text[start + 7 : start + 18],
        widened=text[result.start : result.end],
        expansion=result,
    )
    example = repair.as_example()
    assert example["gainedOnTheLeft"] == "Второе "
    assert example["gainedOnTheRight"].endswith(".")
    assert example["quote"] in repair.widened
