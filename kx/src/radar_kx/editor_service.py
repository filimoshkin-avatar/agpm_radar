"""The one place that shows everything waiting for the owner (slice 2.12).

The owner asked for a single entry point for reading and approving everything
that cannot be put as a multiple-choice question. That is six queues of decisions
and a shelf of documents, behind one address.

Authentication is two things at once, and both are checked here rather than only
in front:

* **HTTP basic**, because that is what a person can use from a browser with no
  token in a URL. Caddy checks it too; this checks it again, because a service
  that trusts a proxy is a service that is open the moment somebody reaches it
  another way.
* **a bearer token**, for the loopback and scripted paths that existed before.

Failed attempts are rate-limited per client. The password is short by the owner's
choice; a public address with a short password and no throttle is found by
scanners in days, and the throttle is the part of that this code can fix.

Everything the page needs is served from here - the HTML, the stylesheet, the
script - so it works under the strict Content-Security-Policy of
radar.agpm.space, which allows no inline script at all.

The actor comes from the credentials, never from the request body: a service that
accepts a name in a body records whatever the caller felt like being.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from radar_kx.config import Settings
from radar_kx.database import Database
from radar_kx.editor_queues import decide as decide_in_queue
from radar_kx.editor_queues import load_queue, queue_summary
from radar_kx.markdown_render import render as render_markdown

MAX_BODY_BYTES = 64 * 1024
PAGE_SIZE = 25

#: Failed attempts allowed from one client before it waits.
MAX_FAILURES = 8
FAILURE_WINDOW_SECONDS = 300.0

_HERE = Path(__file__).resolve().parent
PAGE = (_HERE / "editor_page.html").read_text(encoding="utf-8")
STYLE = (_HERE / "editor_style.css").read_text(encoding="utf-8")
SCRIPT = (_HERE / "editor_app.js").read_text(encoding="utf-8")

#: Where the slice documents live in a deployed release.
DOCS_DIRECTORY = Path("/opt/radar-kx/current/docs")

#: Only documents whose name starts with one of these is listed or served. A
#: directory listing that follows whatever is on disk is a listing that one day
#: follows something else.
DOC_PREFIXES = ("radar-kb-", "radar-v2-kb-")


class Throttle:
    """Per-client failure counter. Not a security boundary, a speed limit."""

    def __init__(
        self, *, limit: int = MAX_FAILURES, window: float = FAILURE_WINDOW_SECONDS
    ) -> None:
        self._limit = limit
        self._window = window
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, client: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            recent = [at for at in self._failures.get(client, []) if moment - at < self._window]
            self._failures[client] = recent
            return len(recent) >= self._limit

    def record_failure(self, client: str, *, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._failures.setdefault(client, []).append(moment)


class EditorService:
    """Request handling, kept out of the HTTP plumbing so it can be tested."""

    def __init__(
        self,
        database: Database,
        *,
        token: str,
        actor: str,
        username: str | None = None,
        password: str | None = None,
        docs_directory: Path = DOCS_DIRECTORY,
    ) -> None:
        if len(token) < 24:
            raise ValueError("the editor token must be at least 24 characters")
        self.database = database
        self._token = token
        self._username = username
        self._password = password
        self.actor = actor
        self.docs_directory = docs_directory
        self.throttle = Throttle()

    def authorized(self, header: str | None) -> bool:
        """Bearer or basic, compared in constant time."""
        if not header:
            return False
        if header.startswith("Bearer "):
            return hmac.compare_digest(header[7:], self._token)
        if header.startswith("Basic ") and self._username and self._password:
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return False
            user, _, secret = decoded.partition(":")
            # Both compared, and both in constant time: comparing the user with ==
            # leaks which half was wrong.
            return hmac.compare_digest(user, self._username) and hmac.compare_digest(
                secret, self._password
            )
        return False

    def summary(self) -> dict[str, Any]:
        return {"queues": queue_summary(self.database)}

    def queue(self, key: str) -> dict[str, Any]:
        return load_queue(self.database, key, limit=PAGE_SIZE)

    def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        return decide_in_queue(
            self.database,
            key=str(payload["queue"]),
            item_id=str(payload["id"]),
            action=str(payload["action"]),
            actor=self.actor,
        )

    def documents(self) -> dict[str, Any]:
        if not self.docs_directory.is_dir():
            return {"documents": []}
        found = [
            path
            for path in sorted(self.docs_directory.glob("*.md"))
            if path.name.startswith(DOC_PREFIXES)
        ]
        return {
            "documents": [
                {
                    "name": path.name,
                    "title": self._title(path),
                    "size": round(path.stat().st_size / 1024),
                }
                for path in found
            ]
        }

    @staticmethod
    def _title(path: Path) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return path.stem

    def document(self, name: str) -> dict[str, Any]:
        # The name is matched against what the listing offered, never joined onto
        # a path: a service that resolves a caller's path serves whatever the
        # caller can name.
        allowed = {item["name"] for item in self.documents()["documents"]}
        if name not in allowed:
            raise KeyError(name)
        path = self.docs_directory / name
        return {"name": name, "html": render_markdown(path.read_text(encoding="utf-8"))}


def make_handler(service: EditorService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "radar-kx-editor"
        sys_version = ""

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            self._send(
                status,
                json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        @property
        def _client(self) -> str:
            forwarded = self.headers.get("X-Forwarded-For", "")
            return (forwarded.split(",")[0].strip() or self.client_address[0])[:64]

        def _authorize(self, token_from_query: str | None = None) -> bool:
            if service.throttle.blocked(self._client):
                self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "too many attempts"})
                return False
            header = self.headers.get("Authorization")
            if header is None and token_from_query:
                header = f"Bearer {token_from_query}"
            if service.authorized(header):
                return True
            service.throttle.record_failure(self._client)
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="Radar", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"
            query = parse_qs(parts.query)
            if not self._authorize(next(iter(query.get("token", [])), None)):
                return
            if path == "/":
                self._send(HTTPStatus.OK, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/style.css":
                self._send(HTTPStatus.OK, STYLE.encode("utf-8"), "text/css; charset=utf-8")
                return
            if path == "/app.js":
                self._send(HTTPStatus.OK, SCRIPT.encode("utf-8"), "text/javascript; charset=utf-8")
                return
            try:
                if path == "/api/summary":
                    self._json(HTTPStatus.OK, service.summary())
                    return
                if path == "/api/queue":
                    self._json(
                        HTTPStatus.OK, service.queue(next(iter(query.get("key", [])), "evidence"))
                    )
                    return
                if path == "/api/docs":
                    self._json(HTTPStatus.OK, service.documents())
                    return
                if path == "/api/doc":
                    name = unquote(next(iter(query.get("name", [])), ""))
                    self._json(HTTPStatus.OK, service.document(name))
                    return
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._authorize():
                return
            if urlsplit(self.path).path.rstrip("/") != "/api/decide":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be an object")
                self._json(HTTPStatus.OK, service.decide(payload))
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            status = args[1] if len(args) > 1 else ""
            print(f"[editor] {self.command} {urlsplit(self.path).path} {status} {self._client}")

    return Handler


def serve(
    settings: Settings,
    *,
    host: str,
    port: int,
    token: str,
    actor: str,
    username: str | None = None,
    password: str | None = None,
    server_factory: Callable[..., ThreadingHTTPServer] = ThreadingHTTPServer,
) -> ThreadingHTTPServer:
    service = EditorService(
        Database(settings), token=token, actor=actor, username=username, password=password
    )
    return server_factory((host, port), make_handler(service))


def generate_token() -> str:
    return secrets.token_urlsafe(32)
