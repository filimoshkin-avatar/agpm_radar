"""Restricted stdin/stdout entrypoint installed behind the Radar deploy SSH key."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser, Namespace
from dataclasses import asdict
from pathlib import Path

from packages.publisher.remote_activation import (
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
        request = read_request(sys.stdin.buffer)
        result = activate_request(
            request,
            content_root=Path("/var/lib/radar-v2/content"),
            incoming_root=Path("/var/lib/radar-v2/incoming/content"),
            audit_root=Path("/var/lib/radar-v2/audit/content"),
            mutation_root=Path("/var/lib/radar-v2/mutation"),
            loopback_url=arguments.loopback_url,
            public_url=arguments.public_url,
            failure_stage=arguments.failure_stage,
        )
    except (RemoteActivationError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 40
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
