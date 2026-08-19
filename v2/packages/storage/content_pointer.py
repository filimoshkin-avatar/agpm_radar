"""Strict parsing for the tiny active-content pointer used by public readers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from packages.storage.safe_files import SafeFilesystemError, read_regular_file, relative_parts

ACTIVE_POINTER_NAME: Final = "active.json"
_ACTIVE_POINTER_MODE: Final = 0o600


class ContentPointerError(RuntimeError):
    """The active content pointer is malformed or escapes its release root."""


@dataclass(frozen=True, slots=True)
class ContentPointer:
    """Validated relative database reference plus the expected release identity."""

    release_id: str
    state_hash: str
    database: str
    database_path: Path


def parse_content_pointer(root: Path, content: bytes) -> ContentPointer:
    """Parse exact pointer fields and bind the database below ``root/releases``."""
    try:
        parsed = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContentPointerError(f"active pointer JSON is invalid: {error}") from error
    if not isinstance(parsed, dict) or set(parsed) != {"database", "releaseId", "stateHash"}:
        raise ContentPointerError("active pointer has unknown or missing fields")
    database = parsed["database"]
    release_id = parsed["releaseId"]
    state_hash = parsed["stateHash"]
    try:
        parts = relative_parts(database) if isinstance(database, str) else ()
    except SafeFilesystemError as error:
        raise ContentPointerError("active pointer database path is invalid") from error
    if len(parts) != 2 or parts[0] != "releases" or not parts[1].endswith(".sqlite"):
        raise ContentPointerError("active pointer database path is outside releases")
    if not isinstance(release_id, str) or not release_id:
        raise ContentPointerError("active pointer releaseId is invalid")
    if (
        not isinstance(state_hash, str)
        or len(state_hash) != 64
        or any(character not in "0123456789abcdef" for character in state_hash)
    ):
        raise ContentPointerError("active pointer stateHash is invalid")
    return ContentPointer(
        release_id=release_id,
        state_hash=state_hash,
        database=database,
        database_path=root.joinpath(*parts),
    )


def read_content_pointer(root: Path) -> ContentPointer:
    """Read a private single-link pointer without following symlinks."""
    return parse_content_pointer(
        root,
        read_regular_file(root / ACTIVE_POINTER_NAME, expected_mode=_ACTIVE_POINTER_MODE),
    )


__all__ = [
    "ACTIVE_POINTER_NAME",
    "ContentPointer",
    "ContentPointerError",
    "parse_content_pointer",
    "read_content_pointer",
]
