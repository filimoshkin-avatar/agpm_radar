"""The web archive rung: text that is gone from the web and kept somewhere (2.3).

One of the two tools the plan lists as missing (§11.6). It answers one question -
"is there a snapshot of this URL, and where" - through the Wayback availability
API, and it answers it in a way the evidence model can use.

What makes an archive snapshot usable as evidence rather than as a rumour is
recorded, not assumed. ADR-0004 rule 21 and migration 004 split the two cases:

* a snapshot whose **URL and capture date are both recorded** is citable, because
  a reader can go and look at the same bytes;
* text taken from an archive whose snapshot was **not** preserved is published
  with a caveat, because we can no longer show anybody what we read.

So this client refuses to return a snapshot it cannot identify. A timestamp with
no URL, or a URL with no timestamp, is not half an answer - it is the difference
between a citation and a claim about a citation.

Retention and rate: the availability API is a public JSON endpoint and this makes
one request per document. There is no crawl here, and no snapshot content is
downloaded by this module - obtaining the text is the fetcher's job, through the
snapshot URL this returns, and it goes through the ordinary ladder with its own
provenance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

AVAILABILITY_ENDPOINT = "https://archive.org/wayback/available"

#: Wayback's timestamp format.
TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"

#: A snapshot older than this is still returned - it is often the only thing
#: there is - but the age travels with it so a caller can weigh it.
DEFAULT_TIMEOUT_SECONDS = 20.0


class WebArchiveError(RuntimeError):
    """The archive could not be asked, or answered something unusable."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One archived capture, identified well enough to be cited."""

    original_url: str
    snapshot_url: str
    captured_at: datetime
    status_code: int

    @property
    def age_days(self) -> int:
        return (datetime.now(UTC) - self.captured_at).days

    def as_json(self) -> dict[str, Any]:
        return {
            "originalUrl": self.original_url,
            "snapshotUrl": self.snapshot_url,
            "capturedAt": self.captured_at.isoformat(),
            "statusCode": self.status_code,
            "ageDays": self.age_days,
        }


def parse_availability(url: str, payload: object) -> Snapshot | None:
    """Read the availability response, or say there is nothing usable.

    Returns ``None`` when the archive has no capture. Raises when it answers with
    something that looks like a capture but cannot be cited: an identifier we
    cannot reproduce is worse than no identifier, because it reads like evidence.
    """
    if not isinstance(payload, dict):
        raise WebArchiveError("availability response is not an object")
    snapshots = payload.get("archived_snapshots")
    if not isinstance(snapshots, dict) or not snapshots:
        return None
    closest = snapshots.get("closest")
    if not isinstance(closest, dict):
        return None
    if not closest.get("available"):
        return None
    snapshot_url = str(closest.get("url") or "")
    timestamp = str(closest.get("timestamp") or "")
    if not snapshot_url or not timestamp:
        raise WebArchiveError(
            "the archive offered a capture without both a URL and a timestamp;"
            " a snapshot that cannot be identified is not evidence"
        )
    try:
        captured_at = datetime.strptime(timestamp, TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise WebArchiveError(f"unreadable capture timestamp {timestamp!r}") from exc
    if snapshot_url.startswith("http://web.archive.org/"):
        snapshot_url = snapshot_url.replace("http://", "https://", 1)
    return Snapshot(
        original_url=url,
        snapshot_url=snapshot_url,
        captured_at=captured_at,
        status_code=int(str(closest.get("status") or 200)),
    )


def find_snapshot(
    url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Snapshot | None:
    """Ask the archive for the closest usable capture of a URL."""
    owned = client is None
    session = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = session.get(AVAILABILITY_ENDPOINT, params={"url": url})
        if response.status_code != 200:
            raise WebArchiveError(f"archive answered {response.status_code}")
        return parse_availability(url, response.json())
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise WebArchiveError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if owned:
            session.close()
