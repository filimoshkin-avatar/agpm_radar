"""Bounded, read-only HTTP-neutral router for the Radar V2 public API."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final
from urllib.parse import parse_qsl, urlsplit

from packages.contracts.json_types import JsonObject
from packages.validation.public_issue import PublicIssueValidationError

from apps.api.database import ActiveDatabaseManager, DatabaseIdentity, PublicDatabaseError
from apps.api.public_data import (
    PublicDataError,
    PublicDataInputError,
    PublicDataRepository,
    PublishedResourceNotFoundError,
)

_MAX_TARGET_BYTES: Final = 4_096
_MAX_QUERY_BYTES: Final = 2_048
_MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
_MAX_OFFSET: Final = 100_000
_DATE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_GAZETTE_PERIOD: Final = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
_IDENTIFIER: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BAD_PERCENT: Final = re.compile(r"%(?![0-9A-Fa-f]{2})")
_REQUEST_COUNTER = itertools.count(1)
_REQUEST_LOCK = threading.Lock()
_API_HEADERS: Final = (
    ("Cache-Control", "no-store"),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
)


class RequestInputError(ValueError):
    """A public request is outside the frozen bounded input contract."""


class SearchRateLimitError(RuntimeError):
    """A remote search key exceeded the bounded in-memory allowance."""


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """Transport-neutral response with pre-encoded body and security headers."""

    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...]


class SearchRateLimiter:
    """Small bounded sliding-window limiter; no query content is retained."""

    def __init__(self, *, requests: int = 30, window_seconds: float = 60.0) -> None:
        if requests < 1 or window_seconds <= 0:
            raise ValueError("rate limiter bounds must be positive")
        self.requests = requests
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            if key not in self._events and len(self._events) >= 1_024:
                oldest_key = min(
                    self._events,
                    key=lambda item: self._events[item][-1] if self._events[item] else 0.0,
                )
                del self._events[oldest_key]
            events = self._events.setdefault(key[:128] or "local", deque())
            cutoff = now - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.requests:
                raise SearchRateLimitError
            events.append(now)


def _request_id(value: str | None) -> str:
    if value is not None and _IDENTIFIER.fullmatch(value) is not None:
        return value
    with _REQUEST_LOCK:
        number = next(_REQUEST_COUNTER)
    return f"req_{number:016x}"


def _json_response(status: int, value: object) -> ApiResponse:
    body = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(body) > _MAX_RESPONSE_BYTES:
        raise PublicDataError("public response exceeds the configured size bound")
    return ApiResponse(
        status=status,
        body=body,
        headers=(("Content-Type", "application/json; charset=utf-8"), *_API_HEADERS),
    )


def _error(status: int, code: str, message: str, request_id: str) -> ApiResponse:
    return _json_response(
        status,
        {"code": code, "message": message, "requestId": request_id},
    )


def _parse_target(raw_target: str) -> tuple[str, dict[str, str]]:
    if not raw_target or len(raw_target.encode("utf-8")) > _MAX_TARGET_BYTES:
        raise RequestInputError("request target is too long")
    parsed = urlsplit(raw_target)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise RequestInputError("request target must be same-origin and fragment-free")
    if "%" in parsed.path or any(ord(character) < 32 for character in parsed.path):
        raise RequestInputError("encoded or control characters are not allowed in API paths")
    query = parsed.query
    if len(query.encode("utf-8")) > _MAX_QUERY_BYTES or _BAD_PERCENT.search(query):
        raise RequestInputError("query string is malformed or too long")
    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=8,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise RequestInputError("query string is malformed") from error
    values: dict[str, str] = {}
    for name, value in pairs:
        if name in values:
            raise RequestInputError(f"duplicate query parameter: {name}")
        if any(ord(character) < 32 for character in name + value):
            raise RequestInputError("query parameters contain control characters")
        values[name] = value
    return parsed.path, values


def _only(values: Mapping[str, str], allowed: set[str]) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise RequestInputError("unknown query parameter")


def _integer(
    values: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdigit() or (len(raw) > 1 and raw.startswith("0")):
        raise RequestInputError(f"{name} must be a canonical integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise RequestInputError(f"{name} is outside its allowed range")
    return value


def _period(values: Mapping[str, str], *, required: bool = False) -> str:
    value = values.get("period")
    if value is None:
        if required:
            raise RequestInputError("period is required")
        return "30d"
    if value not in {"day", "yesterday", "7d", "30d"}:
        raise RequestInputError("period is invalid")
    return value


def _date_value(value: str, label: str) -> str:
    if _DATE.fullmatch(value) is None:
        raise RequestInputError(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise RequestInputError(f"{label} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise RequestInputError(f"{label} must be a canonical ISO date")
    return value


def _text(
    values: Mapping[str, str],
    name: str,
    *,
    required: bool,
    maximum: int,
) -> str | None:
    value = values.get(name)
    if value is None:
        if required:
            raise RequestInputError(f"{name} is required")
        return None
    if not value or len(value) > maximum or any(ord(character) < 32 for character in value):
        raise RequestInputError(f"{name} is empty or too long")
    return value


def _signature(kind: str, values: Mapping[str, str | None]) -> str:
    canonical = json.dumps(
        {"kind": kind, **values},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def _offset_cursor(kind: str, signature: str, offset: int) -> str:
    return f"v1:{kind}:{signature}:{offset}"


def _parse_offset_cursor(
    raw: str | None,
    *,
    kind: str,
    signature: str,
) -> int:
    if raw is None:
        return 0
    parts = raw.split(":")
    if len(parts) != 4 or parts[:3] != ["v1", kind, signature]:
        raise RequestInputError("cursor does not match this query")
    if not parts[3].isdigit() or (len(parts[3]) > 1 and parts[3].startswith("0")):
        raise RequestInputError("cursor offset is invalid")
    offset = int(parts[3])
    if not 1 <= offset <= _MAX_OFFSET:
        raise RequestInputError("cursor offset is outside its allowed range")
    return offset


def _issue_cursor(raw: str | None) -> str | None:
    if raw is None:
        return None
    prefix = "v1:issues:"
    if not raw.startswith(prefix):
        raise RequestInputError("issue cursor is invalid")
    return _date_value(raw.removeprefix(prefix), "issue cursor")


def _gazette_cursor(raw: str | None) -> tuple[str, str] | None:
    if raw is None:
        return None
    parts = raw.split(":", 3)
    if (
        len(parts) != 4
        or parts[:2] != ["v1", "gazettes"]
        or _GAZETTE_PERIOD.fullmatch(parts[2]) is None
        or _IDENTIFIER.fullmatch(parts[3]) is None
    ):
        raise RequestInputError("gazette cursor is invalid")
    return parts[2], parts[3]


class RadarApi:
    """Frozen eleven-endpoint Radar V2 public API."""

    def __init__(
        self,
        manager: ActiveDatabaseManager,
        *,
        application_release_id: str,
        search_limiter: SearchRateLimiter | None = None,
    ) -> None:
        self.manager = manager
        if _IDENTIFIER.fullmatch(application_release_id) is None:
            raise ValueError("application release id is invalid")
        self.application_release_id = application_release_id
        self.search_limiter = search_limiter or SearchRateLimiter()
        #: The issue documents built from the active release, keyed by its state
        #: hash: see `_repository`.
        self._issue_cache: tuple[str, dict[str, JsonObject]] = ("", {})

    def _repository(
        self, connection: sqlite3.Connection, identity: DatabaseIdentity
    ) -> PublicDataRepository:
        """The repository over this connection, with the issues this release has built.

        Building one IssueDetail from the views costs about 6 ms and validates the
        whole document. The 30-day material list built thirty of them on every
        request, under the manager's lock: 170 ms measured on 2026-09-05 against the
        production seed, search the same, the archive list 130 ms. A release never
        changes, so the documents are kept by state hash and dropped with it. This
        runs inside the manager's lock, which is what makes a plain dict enough.
        """
        state_hash, cache = self._issue_cache
        if state_hash != identity.state_hash:
            cache = {}
            self._issue_cache = (identity.state_hash, cache)
        return PublicDataRepository(connection, issue_cache=cache)

    def _dispatch(self, path: str, values: dict[str, str], remote_key: str) -> object:
        if path == "/api/health":
            _only(values, set())
            identity = self.manager.identity()
            result = {
                "databaseStateHash": identity.state_hash,
                "releaseId": identity.release_id,
                "schemaVersion": identity.schema_version,
                "status": "ok",
            }
            result["applicationReleaseId"] = self.application_release_id
            return result
        if path == "/api/latest":
            _only(values, set())
            return self.manager.execute(
                lambda connection, identity: self._repository(connection, identity).latest_issue()
            )
        if path == "/api/issues":
            _only(values, {"cursor", "limit"})
            limit = _integer(values, "limit", default=20, minimum=1, maximum=100)
            before_issue_date = _issue_cursor(values.get("cursor"))
            items, next_date = self.manager.execute(
                lambda connection, identity: self._repository(connection, identity).issues(
                    limit=limit,
                    before_date=before_issue_date,
                )
            )
            return {
                "items": items,
                "nextCursor": f"v1:issues:{next_date}" if next_date is not None else None,
            }
        if path.startswith("/api/issues/"):
            _only(values, set())
            raw_date = path.removeprefix("/api/issues/")
            if "/" in raw_date:
                raise RequestInputError("issue path is invalid")
            issue_date = _date_value(raw_date, "issueDate")
            return self.manager.execute(
                lambda connection, identity: self._repository(connection, identity).issue(
                    issue_date
                )
            )
        if path in {"/api/materials", "/api/search"}:
            allowed = {"cursor", "limit", "period"}
            if path == "/api/materials":
                allowed |= {"perimeter", "rubric"}
            else:
                allowed.add("q")
            _only(values, allowed)
            limit = _integer(values, "limit", default=20, minimum=1, maximum=100)
            period = _period(values)
            perimeter = values.get("perimeter")
            if perimeter is not None and perimeter not in {"near", "mid", "far"}:
                raise RequestInputError("perimeter is invalid")
            rubric = _text(values, "rubric", required=False, maximum=80)
            query = _text(values, "q", required=path == "/api/search", maximum=200)
            kind = "search" if query is not None else "materials"
            signature = _signature(
                kind,
                {
                    "period": period,
                    "perimeter": perimeter,
                    "query": query,
                    "rubric": rubric,
                },
            )
            offset = _parse_offset_cursor(
                values.get("cursor"),
                kind=kind,
                signature=signature,
            )
            if query is not None:
                self.search_limiter.check(remote_key)
            items, next_offset = self.manager.execute(
                lambda connection, identity: self._repository(connection, identity).materials(
                    period=period,
                    perimeter=perimeter,
                    rubric=rubric,
                    query=query,
                    offset=offset,
                    limit=limit,
                )
            )
            return {
                "items": items,
                "nextCursor": (
                    _offset_cursor(kind, signature, next_offset)
                    if next_offset is not None
                    else None
                ),
            }
        if path == "/api/stats":
            _only(values, {"period"})
            period = _period(values, required=True)
            return self.manager.execute(
                lambda connection, identity: self._repository(connection, identity).stats(period)
            )
        if path == "/api/timeseries":
            _only(values, {"basis", "days"})
            days = _integer(values, "days", default=30, minimum=1, maximum=90)
            basis = values.get("basis", "issue")
            if basis not in {"issue", "publication"}:
                raise RequestInputError("basis is invalid")
            items = self.manager.execute(
                lambda connection, identity: self._repository(connection, identity).timeseries(
                    days=days,
                    basis=basis,
                )
            )
            return {"items": items}
        if path == "/api/rubrics":
            _only(values, {"anchor", "period"})
            period = _period(values)
            anchor = _date_value(values["anchor"], "anchor") if "anchor" in values else None
            return self.manager.execute(
                lambda connection, identity: self._repository(connection, identity).rubrics(
                    period, anchor
                )
            )
        if path == "/api/sources":
            _only(values, {"period"})
            period = _period(values)
            return self.manager.execute(
                lambda connection, identity: self._repository(connection, identity).sources(period)
            )
        if path == "/api/gazettes":
            _only(values, {"cursor", "limit"})
            limit = _integer(values, "limit", default=20, minimum=1, maximum=100)
            gazette_before = _gazette_cursor(values.get("cursor"))
            items, next_value = self.manager.execute(
                lambda connection, identity: self._repository(connection, identity).gazettes(
                    limit=limit,
                    before=gazette_before,
                )
            )
            next_cursor = None
            if next_value is not None:
                next_cursor = f"v1:gazettes:{next_value[0]}:{next_value[1]}"
            return {"items": items, "nextCursor": next_cursor}
        raise PublishedResourceNotFoundError("API endpoint not found")

    def handle(
        self,
        method: str,
        raw_target: str,
        *,
        request_id: str | None = None,
        remote_key: str = "local",
    ) -> ApiResponse:
        """Return bounded JSON for one request without exposing exception details."""
        safe_request_id = _request_id(request_id)
        if method != "GET":
            return _error(405, "METHOD_NOT_ALLOWED", "Only GET is supported", safe_request_id)
        try:
            path, values = _parse_target(raw_target)
            return _json_response(200, self._dispatch(path, values, remote_key))
        except (RequestInputError, PublicDataInputError):
            return _error(400, "INVALID_REQUEST", "The request is invalid", safe_request_id)
        except PublishedResourceNotFoundError:
            return _error(404, "NOT_FOUND", "Published resource not found", safe_request_id)
        except SearchRateLimitError:
            return _error(429, "RATE_LIMITED", "Search rate limit exceeded", safe_request_id)
        except (
            PublicDataError,
            PublicDatabaseError,
            PublicIssueValidationError,
            sqlite3.DatabaseError,
        ):
            return _error(
                503, "SERVICE_UNAVAILABLE", "Published data is unavailable", safe_request_id
            )
        except Exception:  # fail closed at the public boundary
            return _error(
                500, "INTERNAL_ERROR", "The request could not be completed", safe_request_id
            )


__all__ = [
    "ApiResponse",
    "RadarApi",
    "RequestInputError",
    "SearchRateLimiter",
]
