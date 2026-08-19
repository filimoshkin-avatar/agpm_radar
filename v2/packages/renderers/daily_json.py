"""Byte-stable public daily JSON rendering for Radar V2."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import cast

from packages.domain.snapshot import JsonObject
from packages.validation.public_issue import (
    build_public_issue,
    validate_public_issue_document,
)


def render_public_issue_json(document: Mapping[str, object]) -> bytes:
    """Render one already projected IssueDetail as canonical UTF-8 JSON."""
    validated = validate_public_issue_document(dict(document))
    return (
        json.dumps(
            validated,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def render_daily_json(connection: sqlite3.Connection, *, issue_date: str) -> bytes:
    """Validate a published SQLite aggregate and render its public JSON artifact."""
    return render_public_issue_json(build_public_issue(connection, issue_date=issue_date))


def parse_public_issue_json(content: bytes) -> JsonObject:
    """Parse canonical public JSON and reject non-canonical or unsafe documents."""
    try:
        value: object = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid public issue JSON: {error}") from error
    validated = validate_public_issue_document(value)
    if render_public_issue_json(cast(dict[str, object], validated)) != content:
        raise ValueError("public issue JSON is not canonical")
    return validated


__all__ = ["parse_public_issue_json", "render_daily_json", "render_public_issue_json"]
