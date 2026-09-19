"""Read-only access to separately published, content-bound observation artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packages.contracts.event_observation import issue_hash, validate_report
from packages.storage.safe_files import read_regular_file

DEFAULT_ROOT = Path("/var/lib/radar-v2/event-observations")


def read_observation(root: Path, issue: dict[str, Any]) -> dict[str, Any]:
    hashed = issue_hash(issue)
    path = root / f"{hashed}.json"
    try:
        report = validate_report(json.loads(read_regular_file(path, expected_mode=0o600)))
        if report["issueHash"] != hashed or report["issueDate"] != issue["issueDate"]:
            raise ValueError("stale observation")
        return report
    except FileNotFoundError:
        return {
            "status": "pending",
            "issueHash": hashed,
            "issueDate": issue["issueDate"],
            "pairs": [],
        }
    except (OSError, RuntimeError, ValueError):
        return {
            "status": "error",
            "issueHash": hashed,
            "issueDate": issue["issueDate"],
            "pairs": [],
        }
