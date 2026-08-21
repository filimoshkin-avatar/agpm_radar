from __future__ import annotations

import email.utils
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from radar_kx.config import Settings
from radar_kx.parser import ParsedContent, parse_content
from radar_kx.url_policy import (
    UnsafeUrlError,
    reddit_json_url,
    resolve_public_url,
    telegram_embed_url,
)

SAFE_RESPONSE_HEADERS = {
    "cache-control",
    "content-language",
    "content-length",
    "content-encoding",
    "content-type",
    "date",
    "etag",
    "expires",
    "last-modified",
    "location",
}


class FetchError(RuntimeError):
    def __init__(self, code: str, detail: str, *, retryable: bool) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail[:4000]
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DocumentTask:
    document_id: str
    canonical_url: str
    attempt_count: int
    etag: str | None
    last_modified: str | None
    # Per-document policy exceptions. Both are recorded in the queue with a reason
    # and default to the global policy; see docs/radar-kx-issue-perimeter-2026-08-21.md.
    robots_override: bool = False
    body_limit_bytes: int | None = None

    @property
    def source_kind(self) -> str:
        return "network_robots_override" if self.robots_override else "network"


@dataclass(frozen=True, slots=True)
class RawResponse:
    requested_url: str
    final_url: str
    started_at: datetime
    fetched_at: datetime
    http_status: int
    content_type: str
    headers: dict[str, str]
    body: bytes | None


@dataclass(frozen=True, slots=True)
class FetchResult:
    task: DocumentTask
    response: RawResponse | None
    parsed: ParsedContent | None
    error_code: str | None
    error_detail: str | None
    retryable: bool
    not_modified: bool


class HostLimiter:
    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            deadline = self._next_allowed.get(host, now)
            delay = max(0.0, deadline - now)
            self._next_allowed[host] = max(now, deadline) + self._interval
        if delay:
            time.sleep(delay)


def _filtered_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in response.headers.items()
        if key.lower() in SAFE_RESPONSE_HEADERS
    }


def _retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return max(0, int((parsed - datetime.now(UTC)).total_seconds()))


def _download(
    *,
    client: httpx.Client,
    limiter: HostLimiter,
    settings: Settings,
    requested_url: str,
    actual_url: str,
    conditional_headers: dict[str, str] | None = None,
    max_redirects: int = 6,
    max_body_bytes: int | None = None,
) -> RawResponse:
    current_url = actual_url
    started_at = datetime.now(UTC)
    limit = max_body_bytes or settings.max_body_bytes
    headers = conditional_headers or {}
    for _ in range(max_redirects + 1):
        resolved = resolve_public_url(current_url)
        limiter.wait(resolved.host)
        try:
            with client.stream("GET", resolved.url, headers=headers) as response:
                status = response.status_code
                safe_headers = _filtered_headers(response)
                fetched_at = datetime.now(UTC)
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("redirect_without_location", resolved.url, retryable=False)
                    current_url = urljoin(resolved.url, location)
                    headers = {}
                    continue
                if status == 304:
                    return RawResponse(
                        requested_url=requested_url,
                        final_url=resolved.url,
                        started_at=started_at,
                        fetched_at=fetched_at,
                        http_status=status,
                        content_type=response.headers.get("content-type", ""),
                        headers=safe_headers,
                        body=None,
                    )
                if status == 429 or status >= 500:
                    retry_after = _retry_after_seconds(response)
                    suffix = f" retry_after={retry_after}" if retry_after is not None else ""
                    raise FetchError(f"http_{status}", resolved.url + suffix, retryable=True)
                if status < 200 or status >= 300:
                    raise FetchError(f"http_{status}", resolved.url, retryable=False)
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        announced = int(content_length)
                    except ValueError:
                        announced = 0
                    if announced > limit:
                        raise FetchError(
                            "body_too_large",
                            f"announced={announced} limit={limit}",
                            retryable=False,
                        )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise FetchError(
                            "body_too_large",
                            f"received>{limit}",
                            retryable=False,
                        )
                    chunks.append(chunk)
                return RawResponse(
                    requested_url=requested_url,
                    final_url=resolved.url,
                    started_at=started_at,
                    fetched_at=fetched_at,
                    http_status=status,
                    content_type=response.headers.get("content-type", ""),
                    headers=safe_headers,
                    body=b"".join(chunks),
                )
        except httpx.TimeoutException as exc:
            raise FetchError("timeout", str(exc), retryable=True) from exc
        except httpx.NetworkError as exc:
            raise FetchError("network_error", str(exc), retryable=True) from exc
        except UnsafeUrlError as exc:
            raise FetchError("unsafe_url", str(exc), retryable=False) from exc
    raise FetchError("too_many_redirects", actual_url, retryable=False)


class RobotsPolicy:
    def __init__(self) -> None:
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._origin_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def allowed(
        self,
        *,
        url: str,
        client: httpx.Client,
        limiter: HostLimiter,
        settings: Settings,
    ) -> bool:
        if not settings.respect_robots:
            return True
        parsed = urlsplit(url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        with self._guard:
            origin_lock = self._origin_locks.setdefault(origin, threading.Lock())
        with origin_lock:
            with self._guard:
                cached = origin in self._cache
                parser = self._cache.get(origin)
            if not cached:
                parser = self._load(
                    origin=origin,
                    client=client,
                    limiter=limiter,
                    settings=settings,
                )
                with self._guard:
                    self._cache[origin] = parser
        return parser is None or parser.can_fetch(settings.user_agent, url)

    @staticmethod
    def _load(
        *,
        origin: str,
        client: httpx.Client,
        limiter: HostLimiter,
        settings: Settings,
    ) -> urllib.robotparser.RobotFileParser | None:
        robots_url = origin + "/robots.txt"
        try:
            response = _download(
                client=client,
                limiter=limiter,
                settings=settings,
                requested_url=robots_url,
                actual_url=robots_url,
                max_redirects=3,
                max_body_bytes=1024 * 1024,
            )
        except FetchError as exc:
            # A real 404 means the origin has no robots policy. Authentication,
            # authorization, and other permanent failures fail closed. Transient
            # failures remain retryable instead of being cached as an allow/deny.
            if exc.code == "http_404":
                return None
            if exc.retryable:
                raise
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser
        if response.body is None:
            return None
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
        return parser


def fetch_document(
    *,
    task: DocumentTask,
    client: httpx.Client,
    limiter: HostLimiter,
    robots: RobotsPolicy,
    settings: Settings,
) -> FetchResult:
    try:
        resolved = resolve_public_url(task.canonical_url)
        if not task.robots_override and not robots.allowed(
            url=resolved.url,
            client=client,
            limiter=limiter,
            settings=settings,
        ):
            raise FetchError("robots_denied", resolved.url, retryable=False)
        actual_url = (
            reddit_json_url(resolved.url) or telegram_embed_url(resolved.url) or resolved.url
        )
        if (
            actual_url != resolved.url
            and not task.robots_override
            and not robots.allowed(
                url=actual_url,
                client=client,
                limiter=limiter,
                settings=settings,
            )
        ):
            raise FetchError("robots_denied", actual_url, retryable=False)
        conditional_headers: dict[str, str] = {}
        if task.etag:
            conditional_headers["if-none-match"] = task.etag
        if task.last_modified:
            conditional_headers["if-modified-since"] = task.last_modified
        response = _download(
            client=client,
            limiter=limiter,
            settings=settings,
            requested_url=resolved.url,
            actual_url=actual_url,
            conditional_headers=conditional_headers,
            max_body_bytes=task.body_limit_bytes,
        )
        if response.http_status == 304:
            return FetchResult(
                task=task,
                response=response,
                parsed=None,
                error_code=None,
                error_detail=None,
                retryable=False,
                not_modified=True,
            )
        if response.body is None:
            raise FetchError("empty_response", response.final_url, retryable=True)
        try:
            parsed = parse_content(
                body=response.body,
                content_type=response.content_type,
                source_url=response.final_url,
                min_text_chars=settings.min_text_chars,
            )
        except Exception as exc:
            # Parser libraries operate on untrusted input. Preserve the fetched body
            # and isolate the document instead of terminating the whole worker run.
            return FetchResult(
                task=task,
                response=response,
                parsed=None,
                error_code="content_parse_error",
                error_detail=f"{type(exc).__name__}: {exc}"[:4000],
                retryable=False,
                not_modified=False,
            )
        error_code = None if parsed.is_complete else "weak_or_missing_text"
        return FetchResult(
            task=task,
            response=response,
            parsed=parsed,
            error_code=error_code,
            error_detail=None if parsed.is_complete else f"quality={parsed.quality}",
            retryable=False,
            not_modified=False,
        )
    except FetchError as exc:
        return FetchResult(
            task=task,
            response=None,
            parsed=None,
            error_code=exc.code,
            error_detail=exc.detail,
            retryable=exc.retryable,
            not_modified=False,
        )
    except (OSError, ValueError) as exc:
        return FetchResult(
            task=task,
            response=None,
            parsed=None,
            error_code="parse_or_io_error",
            error_detail=str(exc)[:4000],
            retryable=False,
            not_modified=False,
        )
