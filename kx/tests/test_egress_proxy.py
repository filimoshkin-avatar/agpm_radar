"""The proxy is the enforcement point for P18. Its refusals are the interesting part."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from radar_kx import egress_proxy
from radar_kx.egress_proxy import (
    ALLOWED_ENDPOINTS,
    EgressRefusedError,
    handle_client,
    parse_connect_target,
    read_request_head,
)


def _head(line: str) -> bytes:
    return f"{line}\r\nHost: example\r\n\r\n".encode("ascii")


def test_the_two_approved_endpoints_are_the_two_the_owner_named() -> None:
    assert frozenset({("api.z.ai", 443), ("api.minimax.io", 443)}) == ALLOWED_ENDPOINTS


@pytest.mark.parametrize(
    "target", ["api.z.ai:443", "API.Z.AI:443", "api.z.ai.:443", "api.minimax.io:443"]
)
def test_an_approved_target_is_accepted_however_it_is_spelled(target: str) -> None:
    host, port = parse_connect_target(_head(f"CONNECT {target} HTTP/1.1"))
    assert (host, port) in ALLOWED_ENDPOINTS


@pytest.mark.parametrize(
    ("line", "outcome"),
    [
        ("CONNECT api.openai.com:443 HTTP/1.1", "refused_target"),
        ("CONNECT api.z.ai:80 HTTP/1.1", "refused_target"),
        ("CONNECT evil.example:443 HTTP/1.1", "refused_target"),
        ("GET https://api.z.ai/v4/chat HTTP/1.1", "refused_method"),
        ("POST http://api.z.ai/ HTTP/1.1", "refused_method"),
        ("CONNECT api.z.ai HTTP/1.1", "refused_malformed"),
        ("CONNECT api.z.ai:https HTTP/1.1", "refused_malformed"),
        ("NONSENSE", "refused_malformed"),
    ],
)
def test_everything_else_is_refused_by_name(line: str, outcome: str) -> None:
    with pytest.raises(EgressRefusedError) as raised:
        parse_connect_target(_head(line))
    assert raised.value.outcome == outcome


def test_an_address_literal_never_matches_even_for_an_approved_host() -> None:
    # What is approved is a name we resolve and check, not whatever a caller can dial.
    with pytest.raises(EgressRefusedError) as raised:
        parse_connect_target(_head("CONNECT 203.0.113.7:443 HTTP/1.1"))
    assert raised.value.outcome == "refused_target"


def test_a_head_that_never_ends_is_refused_rather_than_read_forever() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"CONNECT api.z.ai:443 HTTP/1.1\r\nX: " + b"a" * 9000)
        with pytest.raises(EgressRefusedError) as raised:
            await read_request_head(reader)
        assert raised.value.outcome == "refused_oversize_head"

    asyncio.run(scenario())


async def _serve_once(
    monkeypatch: pytest.MonkeyPatch, request_bytes: bytes, *, upstream: bool
) -> tuple[bytes, list[dict[str, Any]]]:
    """Run one client through a proxy whose allowlist points at a local echo server."""
    records: list[dict[str, Any]] = []
    echo_port = 0
    if upstream:

        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            while data := await reader.read(1024):
                writer.write(data.upper())
                await writer.drain()
            writer.close()

        echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        echo_port = int(echo_server.sockets[0].getsockname()[1])
        monkeypatch.setattr(
            egress_proxy, "ALLOWED_ENDPOINTS", frozenset({("localhost", echo_port)})
        )
        monkeypatch.setattr(egress_proxy, "resolve_public_host", lambda host, port: ("127.0.0.1",))

    async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_client(reader, writer, emit=records.append)

    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request_bytes.replace(b"{PORT}", str(echo_port).encode("ascii")))
    await writer.drain()
    reply = await asyncio.wait_for(reader.read(4096), 5.0)
    if b"200 Connection Established" in reply:
        writer.write(b"hello")
        await writer.drain()
        reply += await asyncio.wait_for(reader.read(4096), 5.0)
    writer.close()
    server.close()
    await server.wait_closed()
    if upstream:
        echo_server.close()
        await echo_server.wait_closed()
    return reply, records


def test_a_refused_target_gets_403_and_a_journal_line(monkeypatch: pytest.MonkeyPatch) -> None:
    reply, records = asyncio.run(
        _serve_once(monkeypatch, b"CONNECT api.openai.com:443 HTTP/1.1\r\n\r\n", upstream=False)
    )
    assert reply.startswith(b"HTTP/1.1 403 Forbidden")
    assert records[0]["outcome"] == "refused_target"
    assert records[0]["host"] is None  # refused before a target was ever accepted
    assert "api.openai.com" in records[0]["detail"]


def test_an_approved_target_is_tunnelled_and_the_bytes_are_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply, records = asyncio.run(
        _serve_once(monkeypatch, b"CONNECT localhost:{PORT} HTTP/1.1\r\n\r\n", upstream=True)
    )
    assert b"200 Connection Established" in reply
    assert reply.endswith(b"HELLO")
    assert records[0]["outcome"] == "allowed"
    assert records[0]["bytesUp"] == 5
    assert records[0]["bytesDown"] == 5
