"""The one process on the KX side with a route off the host, and it knows two names.

Owner decision P18 allows full text to leave Local Ru, and allows it to leave for
exactly two endpoints. ADR-0005 §2.2 asks for that limit to be a property of the
systemd unit rather than a setting of the application, because a setting is one
edit away from being a different setting.

Two layers carry it, and neither is application configuration:

* the Hermes profile unit sets ``IPAddressDeny=any`` with ``IPAddressAllow=localhost``,
  so the process that talks to models cannot open a socket to anything but loopback;
* this proxy refuses every target except the two in :data:`ALLOWED_ENDPOINTS` - a
  frozenset in versioned code that goes through the gates, not a list in a file
  somebody can edit on the host at three in the morning.

It speaks only ``CONNECT``. The tunnel carries the caller's own TLS, so this process
never sees a request body, an API key or a document: it sees a hostname, a byte count
and a duration. That is the intended shape. It is a control, not a second copy of the
evidence store, and there is nothing in it worth stealing.

Every attempt is written to the journal as one JSON line, refusals included. The
orchestrator writes its own row to ``egress_audit`` for the same call, from the other
side of the boundary and knowing things this process cannot know - which run, which
document, how many tokens. Two independent records of one call is the point: either
can be compared against the other, and a call that appears in only one of them is a
finding.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import sys
import time
from collections.abc import Callable
from typing import Any

from radar_kx.url_policy import UnsafeUrlError, resolve_public_host

#: The two endpoints owner decision P9 and ADR-0005 name, as ``(host, port)``.
#: ``api.z.ai`` serves ``zai/glm-5.2``; ``api.minimax.io`` serves
#: ``minimax/MiniMax-M3``. Both are the base URLs the Hermes provider plugins
#: declare, read off the installed Hermes 0.20.0 rather than assumed.
ALLOWED_ENDPOINTS = frozenset({("api.z.ai", 443), ("api.minimax.io", 443)})

#: A CONNECT request head is a request line and a handful of headers. Anything
#: larger is not a client we serve.
MAX_REQUEST_HEAD_BYTES = 8192

#: How long a client has to finish sending its request head.
HEAD_TIMEOUT_SECONDS = 10.0

#: How long the upstream connection may take to establish.
UPSTREAM_TIMEOUT_SECONDS = 15.0

#: How long a tunnel may sit with nothing crossing it in either direction. A
#: model call streams, so silence this long means the far side is gone.
IDLE_TIMEOUT_SECONDS = 300.0

#: Bytes moved per read.
CHUNK_BYTES = 65536

_HEAD_TERMINATOR = b"\r\n\r\n"


class EgressRefusedError(Exception):
    """The request will not be forwarded. ``outcome`` is what the journal records."""

    def __init__(self, outcome: str, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


def parse_connect_target(head: bytes) -> tuple[str, int]:
    """Read ``CONNECT host:port HTTP/1.1`` and return the target, or refuse.

    The comparison against the allowlist is on the literal target the client asked
    for, lowercased. An address literal therefore never matches, even one that
    happens to be where ``api.z.ai`` lives today: what is approved is a name we can
    resolve and check, not whatever a caller can dial directly.
    """
    try:
        request_line = head.split(b"\r\n", 1)[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise EgressRefusedError("refused_malformed", "request line is not ASCII") from exc
    parts = request_line.split(" ")
    if len(parts) != 3:
        raise EgressRefusedError("refused_malformed", f"malformed request line: {request_line!r}")
    method, target, _version = parts
    if method != "CONNECT":
        raise EgressRefusedError("refused_method", f"only CONNECT is served, got {method!r}")
    host, separator, port_text = target.rpartition(":")
    if not separator or not host:
        raise EgressRefusedError("refused_malformed", f"target must be host:port, got {target!r}")
    if not port_text.isascii() or not port_text.isdecimal():
        raise EgressRefusedError("refused_malformed", f"target port is not a number: {target!r}")
    endpoint = (host.lower().rstrip("."), int(port_text))
    if endpoint not in ALLOWED_ENDPOINTS:
        raise EgressRefusedError("refused_target", f"{endpoint[0]}:{endpoint[1]} is not approved")
    return endpoint


async def read_request_head(reader: asyncio.StreamReader) -> bytes:
    """Read up to the blank line that ends the request head."""
    head = b""
    while _HEAD_TERMINATOR not in head:
        if len(head) > MAX_REQUEST_HEAD_BYTES:
            raise EgressRefusedError("refused_oversize_head", "request head exceeds the cap")
        try:
            block = await asyncio.wait_for(reader.read(CHUNK_BYTES), HEAD_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise EgressRefusedError(
                "refused_head_timeout", "client did not finish its head"
            ) from exc
        if not block:
            raise EgressRefusedError("refused_malformed", "client closed before the head ended")
        head += block
    return head


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    moved = 0
    try:
        while True:
            block = await asyncio.wait_for(reader.read(CHUNK_BYTES), IDLE_TIMEOUT_SECONDS)
            if not block:
                break
            writer.write(block)
            await writer.drain()
            moved += len(block)
    except (TimeoutError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        with contextlib.suppress(OSError):
            writer.write_eof()
    return moved


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError):
        writer.close()
        await writer.wait_closed()


def journal_line(record: dict[str, Any]) -> None:
    """Write one attempt to stdout. systemd puts stdout in the journal."""
    sys.stdout.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    emit: Callable[[dict[str, Any]], None] = journal_line,
) -> None:
    started = time.monotonic()
    record: dict[str, Any] = {"event": "egress", "host": None, "port": None}
    try:
        head = await read_request_head(reader)
        host, port = parse_connect_target(head)
        record["host"], record["port"] = host, port
        try:
            addresses = resolve_public_host(host, port)
        except UnsafeUrlError as exc:
            raise EgressRefusedError("refused_address", str(exc)) from exc
        record["addresses"] = list(addresses)
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), UPSTREAM_TIMEOUT_SECONDS
            )
        except (TimeoutError, OSError) as exc:
            raise EgressRefusedError(
                "upstream_unreachable", str(exc) or type(exc).__name__
            ) from exc
    except EgressRefusedError as refusal:
        record["outcome"] = refusal.outcome
        record["detail"] = refusal.detail
        record["durationMs"] = round((time.monotonic() - started) * 1000)
        emit(record)
        with contextlib.suppress(OSError):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
        await _close(writer)
        return

    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    up, down = await asyncio.gather(_pump(reader, upstream_writer), _pump(upstream_reader, writer))
    await _close(upstream_writer)
    await _close(writer)
    record["outcome"] = "allowed"
    record["bytesUp"] = up
    record["bytesDown"] = down
    record["durationMs"] = round((time.monotonic() - started) * 1000)
    emit(record)


async def serve(
    host: str,
    port: int,
    *,
    emit: Callable[[dict[str, Any]], None] = journal_line,
    ready: asyncio.Event | None = None,
) -> None:
    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_client(reader, writer, emit=emit)

    server = await asyncio.start_server(_handle, host, port, family=socket.AF_INET)
    bound = ", ".join(str(sock.getsockname()) for sock in server.sockets)
    emit(
        {
            "event": "listening",
            "address": bound,
            "allowed": sorted(f"{name}:{number}" for name, number in ALLOWED_ENDPOINTS),
        }
    )
    if ready is not None:
        ready.set()
    async with server:
        await server.serve_forever()


def main(argv: tuple[str, ...] = ()) -> None:
    parser = argparse.ArgumentParser(prog="radar-kx-egress-proxy", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19701)
    arguments = parser.parse_args(argv or sys.argv[1:])
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(arguments.host, arguments.port))


if __name__ == "__main__":
    main()
