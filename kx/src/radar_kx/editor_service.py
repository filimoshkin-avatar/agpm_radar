"""The editor's review queue, as something a person can actually work (slice 2.12).

2 769 proposed bindings is not a review anybody can do in a JSON file. This is the
smallest thing that makes it a review: one statement at a time, with the proposed
quotation, its source and its exact offsets beside it, and two buttons.

Three properties it has because of where it sits, not because of care:

* **loopback only.** KX has no public access - no public port, no Caddy route, no
  DNS record (ADR-0005 §16). This binds to 127.0.0.1 and the unit refuses any
  other socket. Reaching it means an SSH tunnel. Putting it behind
  ``radar.agpm.space/editor`` is a separate decision with a separate unit.
* **a bearer token, checked on every request including the page itself.** Not a
  hidden URL. ADR-0006 §7: authorization is server-side on every privileged
  endpoint, never a client check and never an inference from the route.
* **every decision is an append-only event with an actor and a scope**
  (ADR-0006 §3, §12). The service knows who is deciding because the token maps to
  a name; it does not accept a name from the request.

Standard library only. A review tool for one person does not justify a web
framework in the locked requirements, and the dependency would be carried by
every deployment of the worker for the sake of a page.
"""

from __future__ import annotations

import hmac
import json
import secrets
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from radar_kx.config import Settings
from radar_kx.database import Database

#: Largest request body accepted. A decision is a few hundred bytes.
MAX_BODY_BYTES = 64 * 1024

#: How many statements one page of the queue carries.
PAGE_SIZE = 25

#: The interface itself. Kept beside the module rather than inside it: it is a
#: Russian-language HTML page, and a Python linter arguing with Cyrillic UI text
#: teaches nobody anything. It also means the page can be edited without touching
#: the service.
PAGE = (Path(__file__).resolve().parent / "editor_page.html").read_text(encoding="utf-8")


class EditorService:
    """Request handling, kept out of the HTTP plumbing so it can be tested."""

    def __init__(self, database: Database, *, token: str, actor: str) -> None:
        if len(token) < 24:
            # A short token on a service that reads other people's full text is
            # not a token, it is a speed bump.
            raise ValueError("the editor token must be at least 24 characters")
        self.database = database
        self._token = token
        self.actor = actor

    def authorized(self, header: str | None) -> bool:
        """Constant-time comparison, on every request including the page itself."""
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:], self._token)

    def queue(self, query: dict[str, list[str]]) -> dict[str, Any]:
        limit = int((query.get("limit") or [str(PAGE_SIZE)])[0])
        offset = int((query.get("offset") or ["0"])[0])
        return self.database.evidence_queue(limit=max(0, min(limit, 200)), offset=max(0, offset))

    def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        # The actor comes from the token, never from the request. A service that
        # accepts a name in a body records whatever the caller felt like being.
        return self.database.decide_binding(
            concept_claim_id=str(payload["conceptClaimId"]),
            claim_id=str(payload["claimId"]),
            verdict=str(payload["verdict"]),
            actor=self.actor,
            scope="editor",
            rationale=payload.get("rationale"),
        )

    def history(self) -> list[dict[str, Any]]:
        return self.database.editorial_history()


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

        def _authorize(self, token_from_query: str | None = None) -> bool:
            header = self.headers.get("Authorization")
            if header is None and token_from_query:
                header = f"Bearer {token_from_query}"
            if service.authorized(header):
                return True
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            if parts.path == "/":
                # The page itself is privileged: it is the editor interface, and a
                # URL is not a credential.
                if not self._authorize(next(iter(query.get("token", [])), None)):
                    return
                self._send(HTTPStatus.OK, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if not self._authorize():
                return
            if parts.path == "/api/queue":
                self._json(HTTPStatus.OK, service.queue(query))
                return
            if parts.path == "/api/history":
                self._json(HTTPStatus.OK, service.history())
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._authorize():
                return
            if urlsplit(self.path).path != "/api/decide":
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
            # One line per request, to the journal, without the token.
            status = args[1] if len(args) > 1 else ""
            print(f"[editor] {self.command} {urlsplit(self.path).path} {status}")

    return Handler


def serve(
    settings: Settings,
    *,
    host: str,
    port: int,
    token: str,
    actor: str,
    server_factory: Callable[..., ThreadingHTTPServer] = ThreadingHTTPServer,
) -> ThreadingHTTPServer:
    service = EditorService(Database(settings), token=token, actor=actor)
    return server_factory((host, port), make_handler(service))


def generate_token() -> str:
    return secrets.token_urlsafe(32)
