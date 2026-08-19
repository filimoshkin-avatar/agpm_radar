"""Loopback-only stdlib HTTP transport for Radar V2."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, cast

from apps.api.application import RadarApplication

_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})


class RadarHttpServer(ThreadingHTTPServer):
    """HTTP server carrying one explicit application instance."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], application: RadarApplication) -> None:
        self.application = application
        super().__init__(address, RadarRequestHandler)


class RadarRequestHandler(BaseHTTPRequestHandler):
    """Translate GET requests without logging query or material content."""

    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10.0)

    def _respond(self, method: str) -> None:
        server = cast(RadarHttpServer, self.server)
        response = server.application.handle(
            method,
            self.path,
            remote_key=str(self.client_address[0]),
        )
        if method != "GET":
            self.close_connection = True
        self.send_response(response.status)
        for name, value in response.headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        self.wfile.write(response.body)

    def do_GET(self) -> None:
        self._respond("GET")

    def do_POST(self) -> None:
        self._respond("POST")

    def do_PUT(self) -> None:
        self._respond("PUT")

    def do_PATCH(self) -> None:
        self._respond("PATCH")

    def do_DELETE(self) -> None:
        self._respond("DELETE")

    def log_message(self, _format: str, *_args: object) -> None:
        """Suppress raw targets; service logs are added only at the deployment boundary."""


def serve(application: RadarApplication, *, host: str, port: int) -> None:
    """Serve forever on an explicitly loopback host and bounded TCP port."""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("Radar V2 public API must bind to loopback")
    if not 1 <= port <= 65_535:
        raise ValueError("port is outside 1..65535")
    with RadarHttpServer((host, port), application) as server:
        server.serve_forever(poll_interval=0.25)


__all__ = ["RadarHttpServer", "RadarRequestHandler", "serve"]
