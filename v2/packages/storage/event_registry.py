"""Read the event registry from published material evidence in an immutable release."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from packages.contracts.event_dedup import REGISTRY_DAYS, validate_envelope


def published_events(connection: sqlite3.Connection, issue_day: str) -> list[dict[str, Any]]:
    since = (date.fromisoformat(issue_day) - timedelta(days=REGISTRY_DAYS)).isoformat()
    rows = connection.execute(
        """SELECT e.metadata_json FROM material_evidence e
           JOIN issue_materials im ON im.material_id=e.material_id
             AND im.issue_id=json_extract(e.metadata_json, '$.issue_id')
           JOIN issues i ON i.issue_id=im.issue_id
           WHERE e.kind='event_dedup' AND i.lifecycle_status='published'
             AND i.issue_date >= ? AND i.issue_date < ? ORDER BY i.issue_date""",
        (since, issue_day),
    )
    return [validate_envelope(json.loads(row[0])["event_dedup"]) for row in rows]


def issue_evidence(connection: sqlite3.Connection, issue_id: str) -> dict[str, Any]:
    return {
        row[0]: validate_envelope(json.loads(row[1])["event_dedup"])
        for row in connection.execute(
            """SELECT material_id, metadata_json FROM material_evidence
               WHERE kind='event_dedup' AND json_extract(metadata_json, '$.issue_id')=?""",
            (issue_id,),
        )
    }
