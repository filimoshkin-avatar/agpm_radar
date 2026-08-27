"""Rubric dynamics: windows, direction and confidence.

The first implementation handed every rubric of an issue a rise - the index was
hard-wired to 100 - and read confidence as current + previous, so a rubric with
nothing in the previous window came back "high". These are the cases that pin the
repair: comparison against the previous published day, an honest "nothing to
compare with", and confidence bounded by the weaker of the two windows.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from apps.api.public_data import PublicDataInputError, PublicDataRepository
from packages.contracts.json_types import JsonObject
from packages.storage.migrations import create_database

NOW = "2026-08-21T05:10:00Z"
RUBRICS = ("orchestration", "governance", "security")


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _publish(connection: sqlite3.Connection, issue_date: str, rubric_ids: tuple[str, ...]) -> None:
    """Publish an issue carrying one material per entry in rubric_ids."""
    issue_id = f"issue_{issue_date.replace('-', '')}"
    connection.execute(
        """
        INSERT INTO issues VALUES (
          ?, ?, NULL, 'Выпуск Radar', 'Синтетический выпуск.', 'published',
          ?, 'v2', NULL, ?, ?, ?
        )
        """,
        (issue_id, issue_date, f"{issue_date}T05:10:00Z", _hash(issue_id), NOW, NOW),
    )
    for position, rubric_id in enumerate(rubric_ids):
        material_id = f"material_{issue_date.replace('-', '')}_{position}"
        connection.execute(
            """
            INSERT INTO materials VALUES (
              ?, 'Материал радара', ?, ?, 'Synthetic Journal', ?, 'resolved',
              'Краткое содержание.', 'Вывод для AgPM.', 'Короткий сигнал.', ?, ?, ?
            )
            """,
            (
                material_id,
                f"https://example.test/{material_id}",
                f"https://example.test/{material_id}",
                f"{issue_date}T04:00:00Z",
                _hash(material_id),
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO issue_materials VALUES (
              ?, ?, ?, 'near', 'core', 'Краткое содержание.', 'Вывод для AgPM.',
              'Короткий сигнал.', '["Тезис"]', NULL, '[]', 0, 80, 'strong', ?, ?
            )
            """,
            (issue_id, material_id, position, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO material_rubrics VALUES (?, ?, ?, 0.9, 'synthetic')",
            (issue_id, material_id, rubric_id),
        )


@pytest.fixture
def published(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = tmp_path / "rubric-dynamics.sqlite"
    create_database(database, applied_at="2026-08-01T00:00:00Z")
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    for position, rubric_id in enumerate(RUBRICS):
        connection.execute(
            "INSERT INTO rubrics VALUES (?, ?, ?)",
            (rubric_id, f"Рубрика {rubric_id}", position),
        )
    yield connection
    connection.close()


def _by_id(rows: list[JsonObject]) -> dict[str, JsonObject]:
    return {str(row["id"]): row for row in rows}


def test_a_day_is_compared_with_the_previous_published_issue(
    published: sqlite3.Connection,
) -> None:
    # The 24th is a gap in the archive: the 25th has to be compared with the 23rd,
    # or an empty day turns mere presence into growth.
    _publish(published, "2026-08-23", ("orchestration", "orchestration", "governance"))
    _publish(published, "2026-08-25", ("orchestration", "governance", "security"))

    rows = _by_id(PublicDataRepository(published).rubrics("day"))

    assert {row["direction"] for row in rows.values()} != {"up"}
    assert rows["orchestration"]["previousCount"] == 2
    assert rows["orchestration"]["direction"] == "down"
    assert rows["security"]["previousCount"] == 0
    assert rows["governance"]["direction"] == "flat"
    for row in rows.values():
        # Six materials over three rubrics: the prior outweighs the data.
        assert row["confidence"] == "low"


def test_a_day_without_an_earlier_issue_states_that_there_is_nothing_to_compare(
    published: sqlite3.Connection,
) -> None:
    _publish(published, "2026-08-25", ("orchestration", "governance"))

    rows = _by_id(PublicDataRepository(published).rubrics("day"))

    assert set(rows) == {"orchestration", "governance"}
    for row in rows.values():
        assert row["previousShare"] is None
        assert row["index"] == 0.0
        assert row["direction"] == "flat"
        assert row["confidence"] == "low"


def test_confidence_is_bounded_by_the_weaker_window(published: sqlite3.Connection) -> None:
    # The previous window is a stub: two materials against forty. The ratio of
    # shares can be computed; it cannot be trusted.
    _publish(published, "2026-07-30", ("orchestration", "governance"))
    for day in range(1, 21):
        _publish(published, f"2026-08-{day:02d}", ("security", "orchestration"))

    rows = _by_id(PublicDataRepository(published).rubrics("30d", "2026-08-20"))

    assert rows["security"]["previousCount"] == 0
    assert rows["security"]["currentCount"] == 20
    assert rows["security"]["confidence"] == "low"


def test_confidence_rises_when_both_windows_carry_material(
    published: sqlite3.Connection,
) -> None:
    for day in range(1, 29):
        _publish(published, f"2026-07-{day:02d}", ("orchestration",) * 3 + ("governance",) * 2)

    rows = _by_id(PublicDataRepository(published).rubrics("7d", "2026-07-28"))

    assert rows["orchestration"]["confidence"] == "high"
    assert rows["orchestration"]["direction"] == "flat"


def test_rubrics_empty_in_both_windows_are_omitted(published: sqlite3.Connection) -> None:
    # An old issue puts security into the catalog, but it falls outside both
    # windows: a "0 -> flat" row would state what those windows do not hold.
    _publish(published, "2026-06-10", ("security",))
    _publish(published, "2026-08-25", ("orchestration",))

    rows = PublicDataRepository(published).rubrics("7d")

    assert [row["id"] for row in rows] == ["orchestration"]


def test_rubrics_are_ordered_by_current_count_not_by_index(
    published: sqlite3.Connection,
) -> None:
    # Bar length encodes count, so count sets the order. security carries the
    # higher index - nothing in the previous window - and sorting by index would
    # put its two materials above the other rubric's ten.
    _publish(published, "2026-08-13", ("orchestration",) * 10)
    _publish(published, "2026-08-20", ("orchestration",) * 10 + ("security",) * 2)

    rows = PublicDataRepository(published).rubrics("7d")

    assert [row["currentCount"] for row in rows] == [10, 2]
    assert [row["id"] for row in rows] == ["orchestration", "security"]
    assert cast(float, rows[1]["index"]) > cast(float, rows[0]["index"])


def test_an_anchor_after_the_last_issue_is_a_caller_error(
    published: sqlite3.Connection,
) -> None:
    _publish(published, "2026-08-25", ("orchestration",))

    with pytest.raises(PublicDataInputError):
        PublicDataRepository(published).rubrics("day", "2026-09-01")


def test_a_malformed_anchor_is_a_caller_error(published: sqlite3.Connection) -> None:
    _publish(published, "2026-08-25", ("orchestration",))

    with pytest.raises(PublicDataInputError):
        PublicDataRepository(published).rubrics("day", "2026-08-32")


def test_an_anchor_selects_the_archived_issue(published: sqlite3.Connection) -> None:
    _publish(published, "2026-08-24", ("security", "security"))
    _publish(published, "2026-08-25", ("orchestration",))

    rows = _by_id(PublicDataRepository(published).rubrics("day", "2026-08-24"))

    assert cast(str, rows["security"]["anchorDate"]) == "2026-08-24"
    assert rows["security"]["currentCount"] == 2
    assert "orchestration" not in rows
