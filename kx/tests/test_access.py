"""The subscription wall: what a key opens, and what stays open without one.

The owner's split (2026-08-24): the conversation is free, browsing the base is
subscribed. These tests pin the split at the door itself - the HTTP routes -
because a gate that lives only in the interface is a suggestion, not a wall.
"""

from __future__ import annotations

import http.client
import json
import threading
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from typing import Any, cast

from radar_kx.agent_api import (
    KEY_LENGTH,
    KEY_PREFIX,
    SUBSCRIBER_ASKS_PER_CLIENT,
    AgentService,
    AskBudget,
    make_handler,
)
from radar_kx.database import Database


class KeyFake:
    """A service stand-in whose one live key opens everything else."""

    def __init__(self, live: bool) -> None:
        self.live = live
        self.asked: list[dict[str, Any]] = []

    def access(self, authorization: str | None) -> dict[str, Any]:
        from radar_kx.agent_chat import valid_session  # noqa: F401  (shape guard)

        header = (authorization or "").strip()
        key = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""
        digest_ok = key == "radar-" + "x" * (KEY_LENGTH - len(KEY_PREFIX))
        if digest_ok and self.live:
            return {"valid": True, "plan": "full", "expiresAt": "2027-01-01"}
        return {"valid": False, "plan": None, "expiresAt": None}

    # The browsing endpoints, gated by the handler before any of these run.
    def topics(self) -> dict[str, Any]:
        self.asked.append({"path": "topics"})
        return {"topics": []}

    def prompts(self, *, count: int = 6, seed: int | None = None) -> dict[str, Any]:
        return {"prompts": [], "pool": 0, "poolCurated": 0}

    def ask(
        self,
        question: str,
        *,
        client: str,
        admission: str = "knowledge",
        asks_per_client: int | None = None,
    ) -> dict[str, Any]:
        self.asked.append({"path": "ask", "asks_per_client": asks_per_client})
        return {"question": question}

    def guard_check(self, authorization: str | None) -> dict[str, Any]:
        return self.access(authorization)


def _serve(service: Any) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cast(AgentService, service)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(
    connection: http.client.HTTPConnection, path: str, key: str | None = None
) -> tuple[int, Any]:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    body = json.loads(response.read() or b"{}")
    return response.status, body


def test_browsing_without_a_key_meets_one_shape_of_refusal() -> None:
    server, thread = _serve(KeyFake(live=True))
    try:
        host, port = server.server_address[0], server.server_address[1]
        connection = http.client.HTTPConnection(str(host), int(port), timeout=5)
        for path in (
            "/topics",
            "/topics/porogi",
            "/search?q=a",
            "/observatory",
            "/graph",
            "/entities",
            "/contradictions",
            "/gaps",
            "/pages",
            "/pages/wiki/a.md",
            "/statement/c1",
        ):
            status, body = _get(connection, path)
            assert status == HTTPStatus.FORBIDDEN, path
            assert body["error"] == "subscription_required", path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_live_key_opens_browsing_and_reaches_the_wider_window() -> None:
    service = KeyFake(live=True)
    server, thread = _serve(service)
    try:
        host, port = server.server_address[0], server.server_address[1]
        connection = http.client.HTTPConnection(str(host), int(port), timeout=5)
        key = "radar-" + "x" * (KEY_LENGTH - len(KEY_PREFIX))
        status, _ = _get(connection, "/topics", key=key)
        assert status == HTTPStatus.OK
        connection.request(
            "POST",
            "/ask",
            body=json.dumps({"question": "вопрос"}),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        response = connection.getresponse()
        assert response.status == HTTPStatus.OK
        # The wider window reached the pipeline: the handler passed the
        # subscriber's allowance down, not the free default.
        assert service.asked[-1]["asks_per_client"] == SUBSCRIBER_ASKS_PER_CLIENT
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_conversation_paths_stay_open_without_a_key() -> None:
    """The dialogue is the free tier: no key may ever stand in front of it."""
    server, thread = _serve(KeyFake(live=False))
    try:
        host, port = server.server_address[0], server.server_address[1]
        connection = http.client.HTTPConnection(str(host), int(port), timeout=5)
        status, _ = _get(connection, "/health")
        assert status == HTTPStatus.OK
        status, body = _get(connection, "/prompts")
        assert status == HTTPStatus.OK
        assert "prompts" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_free_window_is_what_a_keyless_ask_receives() -> None:
    service = KeyFake(live=False)
    server, thread = _serve(service)
    try:
        host, port = server.server_address[0], server.server_address[1]
        connection = http.client.HTTPConnection(str(host), int(port), timeout=5)
        connection.request(
            "POST",
            "/ask",
            body=json.dumps({"question": "вопрос"}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == HTTPStatus.OK
        assert service.asked[-1]["asks_per_client"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_budget_window_is_shared_not_doubled() -> None:
    """A subscriber's wider ceiling replaces the free one for that call - free
    and keyed asks land in one window, so alternating keys gains nobody."""
    budget = AskBudget(per_client=2, window=300.0, per_day=0)
    assert budget.refused("c") is None  # 1 in the window
    assert budget.refused("c", allowance=5) is None  # 2
    assert budget.refused("c", allowance=5) is None  # 3: past the free ceiling, inside the paid one
    assert budget.refused("c") == "client"  # keyless: the free ceiling of 2 has long passed
    assert budget.refused("c", allowance=5) is None  # 4
    assert budget.refused("c", allowance=5) is None  # 5
    assert budget.refused("c", allowance=5) == "client"  # 6: even the paid ceiling holds


def test_a_key_of_the_wrong_shape_is_refused_before_any_lookup() -> None:
    class Guarded:
        def __init__(self) -> None:
            self.looked_up = 0

        def access_key(self, digest: str) -> dict[str, Any] | None:
            self.looked_up += 1
            return None

    from radar_kx.agent_api import AccessGuard

    database = Guarded()
    guard = AccessGuard(cast(Database, database))
    for probe in (None, "", "Bearer abc", "Bearer radar-short", "Bearer radar-" + "y" * 90):
        assert guard.check(probe)["valid"] is False
    assert database.looked_up == 0


def test_a_valid_answer_is_cached_and_a_revocation_lands_within_the_ttl() -> None:
    class Revocable:
        def __init__(self) -> None:
            self.live = True

        def access_key(self, digest: str) -> dict[str, Any] | None:
            return {"plan": "full", "expires_at": "2027-01-01"} if self.live else None

    from radar_kx.agent_api import AccessGuard

    database = Revocable()
    guard = AccessGuard(cast(Database, database), ttl_seconds=60.0)
    key = "Bearer " + "radar-" + "z" * (KEY_LENGTH - len(KEY_PREFIX))
    assert guard.check(key)["valid"] is True
    database.live = False
    # The cache still answers inside the TTL - approximate, on purpose.
    assert guard.check(key)["valid"] is True
    fresh = AccessGuard(cast(Database, database), ttl_seconds=60.0)
    assert fresh.check(key)["valid"] is False
