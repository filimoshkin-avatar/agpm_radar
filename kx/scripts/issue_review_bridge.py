"""Control-host SSH bridge to owner correction jobs; never prints credentials."""

from __future__ import annotations

import json
import shlex
import sys
import urllib.request
from pathlib import Path


def main() -> None:
    request = json.loads(sys.stdin.buffer.read(65537))
    path = request["path"]
    payload = request.get("payload")
    if path not in {"/api/issue-reviews", "/api/issue-reviews/status"}:
        raise ValueError("unsupported editor bridge action")
    if (path.endswith("/status")) != isinstance(payload, dict):
        raise ValueError("invalid action payload")
    env = dict(
        line.split("=", 1)
        for line in shlex.split(Path("/etc/radar-kx/editor.env").read_text(), comments=True)
        if "=" in line
    )
    token = env["RADAR_KX_EDITOR_TOKEN"]
    call = urllib.request.Request(
        "http://127.0.0.1:19702" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(call, timeout=20) as response:  # noqa: S310
        sys.stdout.buffer.write(response.read(4 * 1024 * 1024))


if __name__ == "__main__":
    main()
