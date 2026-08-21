from __future__ import annotations

import socket
from itertools import pairwise

import pytest

from radar_kx.identifiers import (
    canonicalize_text,
    chunk_text,
    document_id,
    sha256_bytes,
    version_id,
)
from radar_kx.url_policy import (
    UnsafeUrlError,
    normalize_url,
    reddit_json_url,
    resolve_public_url,
    telegram_embed_url,
)


def test_canonicalize_text_is_stable() -> None:
    assert canonicalize_text("  A\r\n\r\n\r\n  Б   В  ") == "A\n\n\nБ В"


def test_canonicalize_text_replaces_postgresql_forbidden_nul_one_for_one() -> None:
    assert canonicalize_text("before\x00after") == "before\ufffdafter"
    assert len(canonicalize_text("before\x00after")) == len("before\x00after")


def test_identifiers_are_deterministic() -> None:
    url = "https://example.com/article"
    identifier = document_id(url)
    assert identifier == sha256_bytes(url.encode())
    assert version_id(document=identifier, raw_sha256="a" * 64, text_sha256="b" * 64) == (
        version_id(document=identifier, raw_sha256="a" * 64, text_sha256="b" * 64)
    )
    historical = version_id(
        document=identifier,
        raw_sha256="a" * 64,
        text_sha256="b" * 64,
        parser_config_sha256="c" * 64,
    )
    assert historical != version_id(
        document=identifier,
        raw_sha256="a" * 64,
        text_sha256="b" * 64,
    )


def test_chunks_cover_canonical_text_without_overlap() -> None:
    text = "Paragraph one. " * 300 + "\n\n" + "Paragraph two. " * 300
    chunks = chunk_text("a" * 64, text, max_chars=1000)
    assert len(chunks) > 2
    assert "".join(chunk.text for chunk in chunks) == text
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(text)
    assert all(left.char_end == right.char_start for left, right in pairwise(chunks))


def test_normalize_url_removes_tracking_and_credentials() -> None:
    assert normalize_url("HTTPS://Example.COM:443/a?utm_source=x&keep=1#fragment") == (
        "https://example.com/a?keep=1"
    )
    with pytest.raises(UnsafeUrlError, match="user information"):
        normalize_url("https://user:password@example.com/")


def test_reddit_json_adapter() -> None:
    assert reddit_json_url("https://www.reddit.com/r/x/comments/abc/title/") == (
        "https://www.reddit.com/r/x/comments/abc/title.json?raw_json=1"
    )
    assert reddit_json_url("https://example.com/r/x/comments/abc/title/") is None


def test_telegram_embed_adapter() -> None:
    assert telegram_embed_url("https://t.me/example_channel/123") == (
        "https://t.me/example_channel/123?embed=1&mode=tme"
    )
    assert telegram_embed_url("https://t.me/example_channel") is None
    assert telegram_embed_url("https://example.com/example_channel/123") is None


def test_resolver_rejects_private_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(UnsafeUrlError, match="non-public"):
        resolve_public_url("https://example.com/")
