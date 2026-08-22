#!/usr/bin/env python3
"""Refuse to start the extraction profile if it is not the profile that was approved.

ADR-0005 §9 lists a fail-closed contract verifier among the things to reuse from NRD
rather than rediscover. This is the Radar KX one. It runs as ``ExecStartPre`` under
the profile's own runtime, and every failure exits 78 - the code the unit marks
``RestartPreventExitStatus``, so a drifted profile stays down and is noticed instead
of restarting every five seconds against a widened allowlist.

What it is defending against is not malice, it is ordinary drift: a Hermes upgrade
that moves the handler the model allowlist patches, an edit to ``config.yaml`` that
adds a third model, an env file that lost a key, a missing ``HTTPS_PROXY`` that would
send model traffic at the internet directly instead of through the one proxy that
knows which two endpoints are approved.

Checks are semantic as well as hash-based on purpose. A hash says the file is the
file the contract expects; it says nothing if the contract and the file were widened
together. So ``model_routes`` is also read and compared against the two models P9
names, spelled out here rather than taken from the contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

#: Owner decision P9. Spelled out here so that widening the contract is not enough
#: to widen the profile: both this file and the contract would have to change, and
#: both go through the repository gates.
APPROVED_MODEL_ROUTES = {"glm-5.2": "zai", "MiniMax-M3": "minimax"}

EXIT_CONTRACT_VIOLATION = 78

_failures: list[str] = []


def require(condition: object, code: str) -> None:
    if not condition:
        _failures.append(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_runtime(contract: dict[str, Any]) -> None:
    hermes = contract.get("hermes")
    if not isinstance(hermes, dict):
        require(False, "hermes_contract_missing")
        return
    for key in ("executable", "python"):
        path = Path(str(hermes.get(key, "")))
        require(path.is_file() and os.access(path, os.X_OK), f"runtime_{key}_not_executable")
    root = Path(str(hermes.get("implementation_root", "")))
    require(root.is_dir(), "hermes_implementation_root_missing")

    # The model allowlist patch reaches into these handlers by name. If an upgrade
    # renames one, the patch silently covers less than it claims to, so refuse.
    module = root / str(hermes.get("api_server_module", ""))
    handlers = hermes.get("patched_handlers")
    if not module.is_file() or not isinstance(handlers, list):
        require(False, "api_server_module_unreadable")
        return
    source = module.read_text(encoding="utf-8", errors="replace")
    for handler in handlers:
        require(f"def {handler}" in source, f"patched_handler_absent:{handler}")


def check_files(profile_home: Path, contract: dict[str, Any]) -> None:
    files = contract.get("files")
    if not isinstance(files, dict) or not files:
        require(False, "files_contract_missing")
        return
    for name, expected in files.items():
        path = profile_home / str(name)
        if not path.is_file():
            require(False, f"profile_file_missing:{name}")
            continue
        require(sha256_file(path) == expected, f"profile_file_changed:{name}")


def check_config(profile_home: Path) -> None:
    path = profile_home / "config.yaml"
    if not path.is_file():
        require(False, "config_missing")
        return
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        require(False, "config_unparseable")
        return
    if not isinstance(config, dict):
        require(False, "config_not_mapping")
        return
    try:
        routes = config["platforms"]["api_server"]["extra"]["model_routes"]
    except (KeyError, TypeError):
        require(False, "model_routes_missing")
        return
    if not isinstance(routes, dict):
        require(False, "model_routes_not_mapping")
        return
    require(set(routes) == set(APPROVED_MODEL_ROUTES), "model_routes_not_the_two_approved")
    for alias, provider in APPROVED_MODEL_ROUTES.items():
        entry = routes.get(alias)
        if not isinstance(entry, dict):
            require(False, f"model_route_malformed:{alias}")
            continue
        require(entry.get("provider") == provider, f"model_route_provider_wrong:{alias}")
        require(entry.get("allow_fallback") is False, f"model_route_fallback_open:{alias}")

    agent = config.get("agent")
    require(isinstance(agent, dict) and agent.get("max_turns") == 1, "agent_not_single_turn")
    disabled = agent.get("disabled_toolsets") if isinstance(agent, dict) else None
    require(isinstance(disabled, list) and "web" in disabled, "web_toolset_not_disabled")
    require(isinstance(disabled, list) and "browser" in disabled, "browser_toolset_not_disabled")
    require(isinstance(disabled, list) and "terminal" in disabled, "terminal_toolset_not_disabled")
    memory = config.get("memory")
    require(isinstance(memory, dict) and memory.get("memory_enabled") is False, "memory_enabled")
    require(config.get("mcp_servers") == {}, "mcp_servers_configured")


def check_environment(contract: dict[str, Any]) -> None:
    env_contract = contract.get("env_file")
    names = env_contract.get("secret_names") if isinstance(env_contract, dict) else None
    if not isinstance(names, list) or not names:
        require(False, "env_contract_missing")
        return
    for name in names:
        require(os.environ.get(str(name), "").strip(), f"secret_absent:{name}")

    egress = contract.get("egress")
    if not isinstance(egress, dict):
        require(False, "egress_contract_missing")
        return
    proxy = str(egress.get("proxy_url", ""))
    require(proxy, "egress_proxy_url_missing_from_contract")
    for variable in ("HTTPS_PROXY", "HTTP_PROXY"):
        require(os.environ.get(variable, "") == proxy, f"{variable}_is_not_the_egress_proxy")
    # NO_PROXY must not carve the approved endpoints back out into a direct route.
    exempted = {item.strip().lower() for item in os.environ.get("NO_PROXY", "").split(",")}
    for endpoint in egress.get("allowed_endpoints", []):
        host = str(endpoint).rsplit(":", 1)[0].lower()
        require(host not in exempted, f"no_proxy_exempts_an_approved_endpoint:{host}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_profile")
    parser.add_argument("--profile-home", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    arguments = parser.parse_args(argv)

    try:
        contract = json.loads(arguments.contract.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"contract_unreadable:{type(exc).__name__}", file=sys.stderr)
        return EXIT_CONTRACT_VIOLATION
    if not isinstance(contract, dict):
        print("contract_not_mapping", file=sys.stderr)
        return EXIT_CONTRACT_VIOLATION

    profile_home = arguments.profile_home
    require(profile_home.is_dir(), "profile_home_missing")
    require(
        os.environ.get("HERMES_HOME", "") == str(profile_home),
        "hermes_home_is_not_the_profile_home",
    )
    check_runtime(contract)
    check_files(profile_home, contract)
    check_config(profile_home)
    check_environment(contract)

    if _failures:
        for failure in sorted(set(_failures)):
            print(failure, file=sys.stderr)
        return EXIT_CONTRACT_VIOLATION
    print(json.dumps({"profile": contract.get("profile"), "verified": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
