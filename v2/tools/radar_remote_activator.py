"""Restricted stdin/stdout entrypoint installed behind the Radar deploy SSH key."""

from __future__ import annotations

import io
import json
import pwd
import sys
from argparse import ArgumentParser, Namespace
from dataclasses import asdict
from pathlib import Path

from packages.contracts.event_observation import MAX_BYTES
from packages.publisher.event_observation import install_observation
from packages.publisher.remote_activation import (
    MAX_REQUEST_BYTES,
    RemoteActivationError,
    activate_request,
    read_request,
)


def _arguments() -> Namespace:
    parser = ArgumentParser()
    parser.add_argument("--loopback-url", default="http://127.0.0.1:8765/api/health")
    parser.add_argument("--public-url", default="https://radar.agpm.space/api/health")
    parser.add_argument("--failure-stage", choices=("loopback", "public"))
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    try:
        identity = pwd.getpwnam("radar-v2-api")
        content = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(content) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds size limit")
        raw = json.loads(content)
        if isinstance(raw, dict) and raw.get("action") == "event_observation":
            if set(raw) != {"action", "report"} or len(content) > MAX_BYTES:
                raise ValueError("invalid observation request")
            stored = install_observation(
                raw["report"],
                content_root=Path("/var/lib/radar-v2/content"),
                root=Path("/var/lib/radar-v2/event-observations"),
                api_uid=identity.pw_uid,
                api_gid=identity.pw_gid,
            )
            print(json.dumps(stored, sort_keys=True))
            return 0
        request = read_request(io.BytesIO(content))
        result = activate_request(
            request,
            content_root=Path("/var/lib/radar-v2/content"),
            incoming_root=Path("/var/lib/radar-v2/incoming/content"),
            audit_root=Path("/var/lib/radar-v2/audit/content"),
            mutation_root=Path("/var/lib/radar-v2/mutation"),
            gazette_root=Path("/var/lib/radar-v2/gazettes/releases"),
            api_uid=identity.pw_uid,
            api_gid=identity.pw_gid,
            loopback_url=arguments.loopback_url,
            public_url=arguments.public_url,
            failure_stage=arguments.failure_stage,
        )
    except (KeyError, RemoteActivationError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 40
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
