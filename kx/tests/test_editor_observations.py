"""Owner observation proxy authenticates before fetching any report."""

from __future__ import annotations

import base64
import io
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import Any, cast

import pytest

from radar_kx.database import Database
from radar_kx.editor_service import EditorService, make_handler


def test_owner_proxy_authenticates_and_does_not_expose_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "test-editor-" * 4
    service = EditorService(
        cast(Database, object()),
        token=token,
        actor="owner",
        username="test-user",
        password="test-password",
    )
    calls: list[Any] = []

    def fetch(request: Any, *, timeout: int) -> io.BytesIO:
        calls.append(request)
        assert timeout == 15
        assert request.full_url == "http://127.0.0.1:8765/api/owner/event-observations/2026-09-19"
        assert request.get_header("Authorization") == "Bearer " + token
        return io.BytesIO(json.dumps({"issueDate": "2026-09-19", "status": "complete"}).encode())

    monkeypatch.setattr("radar_kx.editor_service.urllib.request.urlopen", fetch)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        path = "/api/event-observations?date=2026-09-19"
        client.request("GET", path)
        response = client.getresponse()
        assert response.status == 401
        response.read()
        assert calls == []
        client.request("GET", path, headers={"Authorization": "Bearer wrong"})
        response = client.getresponse()
        assert response.status == 401
        response.read()
        assert calls == []
        basic = "Basic " + base64.b64encode(b"test-user:test-password").decode()
        client.request("GET", path, headers={"Authorization": basic})
        response = client.getresponse()
        assert response.status == 200
        assert response.getheader("Cache-Control") == "no-store"
        content = response.read()
        assert json.loads(content)["status"] == "complete"
        assert token.encode() not in content
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("value", ["../../health", "2026-09-19?token=secret", "2026-99-99"])
def test_observation_proxy_rejects_non_dates(value: str) -> None:
    service = EditorService(cast(Database, object()), token="test-editor-" * 4, actor="owner")
    with pytest.raises(ValueError):
        service.event_observations(value)
