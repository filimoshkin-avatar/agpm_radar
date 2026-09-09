"""Source families are merged before ranking, within the selected issue window."""

from __future__ import annotations

import sqlite3
from typing import cast

from apps.api.public_data import PublicDataRepository


def test_digest_titles_merge_before_sorting_without_merging_other_feeds() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE pub_issues_v1 (issue_id TEXT, issue_date TEXT);
            CREATE TABLE pub_issue_materials_v1 (issue_id TEXT, source_name TEXT);
            INSERT INTO pub_issues_v1 VALUES ('old', '2026-07-01'), ('week', '2026-09-03'),
                ('month', '2026-08-15'), ('current', '2026-09-09');
            """
        )
        rows = [
            *(("week", "AI Agents Directory: ежедневный news brief: First") for _ in range(3)),
            *(("week", "AI Agents Directory: ежедневный news brief: Second") for _ in range(3)),
            *(("month", "AI Agents Directory: ежедневный news brief: Third") for _ in range(3)),
            *(("current", "Other publisher") for _ in range(5)),
            ("old", "Archive only"),
            ("week", "AI Agents Directory Labs"),
            ("week", "Habr: feed A"),
            ("week", "Habr: feed B"),
            ("week", "Perplexity fresh web research: near"),
            ("week", "Perplexity fresh web research: far"),
        ]
        connection.executemany("INSERT INTO pub_issue_materials_v1 VALUES (?, ?)", rows)
        repository = PublicDataRepository(connection)
        month = repository.sources("30d")
        assert month[0] == {"name": "AI Agents Directory", "included": 9}
        assert month[1] == {"name": "Other publisher", "included": 5}
        assert sum(cast(int, row["included"]) for row in month) == 19
        assert len({row["name"] for row in month}) == len(month)
        names = {row["name"] for row in month}
        assert {"AI Agents Directory Labs", "Habr: feed A", "Habr: feed B"} <= names
        assert {"Perplexity · близкий", "Perplexity · дальний"} <= names
        assert repository.sources("7d")[0] == {"name": "AI Agents Directory", "included": 6}
        assert repository.sources("day") == [{"name": "Other publisher", "included": 5}]
        assert repository.sources("day", "2026-07-01") == [{"name": "Archive only", "included": 1}]
        assert repository.sources("day", "2026-07-02") == []
        assert repository.sources("yesterday") == []
    finally:
        connection.close()
