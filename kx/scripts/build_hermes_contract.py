#!/usr/bin/env python3
"""Write the extraction profile's verification contract from the files it describes.

The contract exists so ``verify_profile.py`` can refuse to start a profile whose
files are not the ones that went through the gates. That only works if the hashes in
it are current, so they are generated here and a test asserts the shipped contract
matches - editing ``run_api_only.py`` and forgetting the contract fails the gate
rather than shipping a check that passes because it checks nothing.

The installed Hermes is not a git checkout, so there is no upstream revision to pin
and the contract does not claim one. What it pins instead is the thing that actually
breaks: the three handler names the model allowlist patch reaches into. If an upgrade
renames one, the patch would cover less than it claims and the verifier refuses.

Run with ``--check`` to compare instead of write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROFILE_DIRECTORY = Path(__file__).resolve().parent.parent / "deploy" / "hermes-extraction"
CONTRACT_PATH = PROFILE_DIRECTORY / "verification-contract.json"
HASHED_FILES = ("config.yaml", "run_api_only.py")

PROFILE_HOME = "/var/lib/radar-hermes/profiles/extraction"
RUNTIME = "/usr/local/lib/radar-hermes-runtime/venv/bin"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build() -> dict[str, Any]:
    return {
        "schema_version": "radar-kx-hermes-extraction/v1",
        "slice": "2.0",
        "profile": "extraction",
        "profile_home": PROFILE_HOME,
        "hermes": {
            "version": "0.20.0",
            "config_version": 33,
            "implementation_root": "/usr/local/lib/hermes-agent",
            "api_server_module": "gateway/platforms/api_server.py",
            "patched_handlers": [
                "_handle_chat_completions",
                "_handle_responses",
                "_handle_runs",
            ],
            "executable": f"{RUNTIME}/hermes-radar-kx",
            "python": f"{RUNTIME}/python-radar-kx",
        },
        "api": {
            "host": "127.0.0.1",
            "port": 19700,
            "model_name": "radar-kx-extraction",
            "key_secret_name": "API_SERVER_KEY",
        },
        "egress": {
            "proxy_url": "http://127.0.0.1:19701",
            "allowed_endpoints": ["api.z.ai:443", "api.minimax.io:443"],
        },
        "env_file": {
            "path": "/etc/radar-kx/hermes-extraction.env",
            "owner": "root",
            "mode": "0600",
            "secret_names": ["API_SERVER_KEY", "GLM_API_KEY", "MINIMAX_API_KEY"],
        },
        "model_routes": {"glm-5.2": "zai", "MiniMax-M3": "minimax"},
        "files": {name: _sha256(PROFILE_DIRECTORY / name) for name in HASHED_FILES},
    }


def render(contract: dict[str, Any]) -> str:
    return json.dumps(contract, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(prog="build_hermes_contract")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    expected = render(build())
    if not arguments.check:
        CONTRACT_PATH.write_text(expected, encoding="utf-8")
        print(f"wrote {CONTRACT_PATH}")
        return 0
    actual = CONTRACT_PATH.read_text(encoding="utf-8") if CONTRACT_PATH.is_file() else ""
    if actual != expected:
        print(
            "verification-contract.json is stale; run scripts/build_hermes_contract.py",
            file=sys.stderr,
        )
        return 1
    print("verification-contract.json is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
