from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from radar_kx.config import Settings
from radar_kx.fetcher import (
    DocumentTask,
    HostLimiter,
    RobotsPolicy,
    fetch_document,
)
from radar_kx.url_policy import ResolvedUrl


def _settings(tmp_path: Path, *, max_body_bytes: int = 100_000) -> Settings:
    return Settings(
        dsn="",
        release_id="test",
        capacity_path=str(tmp_path),
        user_agent="Radar-KX-Test/1.0",
        request_timeout_seconds=5,
        connect_timeout_seconds=5,
        per_host_interval_seconds=0.001,
        max_body_bytes=max_body_bytes,
        min_text_chars=30,
        min_free_bytes=1,
        lease_seconds=60,
        max_attempts=2,
        max_in_flight_per_host=8,
        respect_robots=False,
    )


def _public_resolver(value: str) -> ResolvedUrl:
    return ResolvedUrl(url=value, host="example.com", addresses=("203.0.113.1",))


def test_fetch_document_parses_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("radar_kx.fetcher.resolve_public_url", _public_resolver)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8", "etag": '"v1"'},
            text=(
                "<article><h1>Title</h1><p>A sufficiently long evidence paragraph "
                "for testing the parser.</p></article>"
            ),
            request=request,
        )

    settings = _settings(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_document(
            task=DocumentTask("a" * 64, "https://example.com/a", 1, None, None),
            client=client,
            limiter=HostLimiter(settings.per_host_interval_seconds),
            robots=RobotsPolicy(),
            settings=settings,
        )
    assert result.error_code is None
    assert result.parsed is not None and result.parsed.is_complete
    assert result.response is not None and result.response.headers["etag"] == '"v1"'


def test_fetch_document_rejects_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("radar_kx.fetcher.resolve_public_url", _public_resolver)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-length": "5000"},
            content=b"small",
            request=request,
        )

    settings = _settings(tmp_path, max_body_bytes=100)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_document(
            task=DocumentTask("a" * 64, "https://example.com/a", 1, None, None),
            client=client,
            limiter=HostLimiter(settings.per_host_interval_seconds),
            robots=RobotsPolicy(),
            settings=settings,
        )
    assert result.error_code == "body_too_large"
    assert not result.retryable


def test_robots_404_allows_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("radar_kx.fetcher.resolve_public_url", _public_resolver)
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="A complete article body that is long enough for the parser to accept.",
            request=request,
        )

    settings = replace(_settings(tmp_path), respect_robots=True)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_document(
            task=DocumentTask("a" * 64, "https://example.com/a", 1, None, None),
            client=client,
            limiter=HostLimiter(settings.per_host_interval_seconds),
            robots=RobotsPolicy(),
            settings=settings,
        )
    assert requested_paths == ["/robots.txt", "/a"]
    assert result.error_code is None


def test_robots_403_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("radar_kx.fetcher.resolve_public_url", _public_resolver)
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(403, request=request)

    settings = replace(_settings(tmp_path), respect_robots=True)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_document(
            task=DocumentTask("a" * 64, "https://example.com/a", 1, None, None),
            client=client,
            limiter=HostLimiter(settings.per_host_interval_seconds),
            robots=RobotsPolicy(),
            settings=settings,
        )
    assert requested_paths == ["/robots.txt"]
    assert result.error_code == "robots_denied"
    assert not result.retryable


def test_transient_robots_failure_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("radar_kx.fetcher.resolve_public_url", _public_resolver)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    settings = replace(_settings(tmp_path), respect_robots=True)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_document(
            task=DocumentTask("a" * 64, "https://example.com/a", 1, None, None),
            client=client,
            limiter=HostLimiter(settings.per_host_interval_seconds),
            robots=RobotsPolicy(),
            settings=settings,
        )
    assert result.error_code == "http_503"
    assert result.retryable


def test_audited_robots_override_skips_robots_and_labels_its_own_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("radar_kx.fetcher.resolve_public_url", _public_resolver)
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /", request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="A complete article body that is long enough for the parser to accept.",
            request=request,
        )

    settings = replace(_settings(tmp_path), respect_robots=True)
    task = DocumentTask("a" * 64, "https://example.com/a", 1, None, None, robots_override=True)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_document(
            task=task,
            client=client,
            limiter=HostLimiter(settings.per_host_interval_seconds),
            robots=RobotsPolicy(),
            settings=settings,
        )
    assert requested_paths == ["/a"]
    assert result.error_code is None
    assert task.source_kind == "network_robots_override"
    assert DocumentTask("a" * 64, "https://example.com/a", 1, None, None).source_kind == "network"


def test_per_document_body_limit_overrides_the_global_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("radar_kx.fetcher.resolve_public_url", _public_resolver)
    text = "A complete article body that is long enough for the parser to accept. " * 20

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text=text,
            request=request,
        )

    settings = _settings(tmp_path, max_body_bytes=100)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        denied = fetch_document(
            task=DocumentTask("a" * 64, "https://example.com/a", 1, None, None),
            client=client,
            limiter=HostLimiter(settings.per_host_interval_seconds),
            robots=RobotsPolicy(),
            settings=settings,
        )
        allowed = fetch_document(
            task=DocumentTask(
                "a" * 64, "https://example.com/a", 1, None, None, body_limit_bytes=100_000
            ),
            client=client,
            limiter=HostLimiter(settings.per_host_interval_seconds),
            robots=RobotsPolicy(),
            settings=settings,
        )
    assert denied.error_code == "body_too_large"
    assert allowed.error_code is None
    assert allowed.parsed is not None and allowed.parsed.is_complete


def test_robots_policy_loads_distinct_origins_concurrently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    barrier = threading.Barrier(2)
    loaded_origins: list[str] = []

    def fake_load(*, origin: str, **_kwargs: object) -> None:
        loaded_origins.append(origin)
        barrier.wait(timeout=2)

    monkeypatch.setattr(RobotsPolicy, "_load", staticmethod(fake_load))
    settings = replace(_settings(tmp_path), respect_robots=True)
    policy = RobotsPolicy()
    limiter = HostLimiter(settings.per_host_interval_seconds)
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))) as client,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        results = list(
            executor.map(
                lambda url: policy.allowed(
                    url=url,
                    client=client,
                    limiter=limiter,
                    settings=settings,
                ),
                ("https://alpha.example/article", "https://beta.example/article"),
            )
        )
    assert results == [True, True]
    assert sorted(loaded_origins) == ["https://alpha.example", "https://beta.example"]


def test_parser_failure_preserves_response_and_isolates_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("radar_kx.fetcher.resolve_public_url", _public_resolver)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=b"retained evidence body",
            request=request,
        )

    def broken_parser(**_kwargs: object) -> object:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr("radar_kx.fetcher.parse_content", broken_parser)
    settings = _settings(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_document(
            task=DocumentTask("a" * 64, "https://example.com/a", 1, None, None),
            client=client,
            limiter=HostLimiter(settings.per_host_interval_seconds),
            robots=RobotsPolicy(),
            settings=settings,
        )
    assert result.error_code == "content_parse_error"
    assert result.response is not None and result.response.body == b"retained evidence body"
    assert result.parsed is None


def test_telegram_post_uses_embed_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("radar_kx.fetcher.resolve_public_url", _public_resolver)
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="""
            <html><body>
              <div class="tgme_widget_message_author_name">Radar channel</div>
              <div class="tgme_widget_message_text">A complete Telegram message with enough
              evidence text for the production parser and exact provenance.</div>
            </body></html>
            """,
            request=request,
        )

    settings = _settings(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_document(
            task=DocumentTask("a" * 64, "https://t.me/radar_channel/123", 1, None, None),
            client=client,
            limiter=HostLimiter(settings.per_host_interval_seconds),
            robots=RobotsPolicy(),
            settings=settings,
        )
    assert requested_urls == ["https://t.me/radar_channel/123?embed=1&mode=tme"]
    assert result.error_code is None
    assert result.parsed is not None and result.parsed.quality == "telegram_html"
