"""The extraction profile's deploy assets are part of the product, so they are gated.

Three of these tests exist because the same rule is written in more than one place and
the places can drift: P9's two models appear in the profile config, in the verifier
that refuses to start without them, in the generated contract and in the orchestrator
that asks. Three layers of enforcement is deliberate (ADR-0005 §9); three layers that
disagree would be worse than one.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import build_hermes_contract

from radar_kx.egress_proxy import ALLOWED_ENDPOINTS
from radar_kx.orchestrator import ALLOWED_MODELS

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
PROFILE = DEPLOY / "hermes-extraction"
CONTRACT = json.loads((PROFILE / "verification-contract.json").read_text(encoding="utf-8"))


def _unit(name: str) -> list[str]:
    return (DEPLOY / name).read_text(encoding="utf-8").splitlines()


def _config_model_aliases() -> set[str]:
    """The keys directly under ``model_routes:``, read by indentation.

    pyyaml is not a dependency of this project and adding one to read eight lines
    would be the wrong trade. The block's shape is fixed by the file next to this
    test, and a change to that shape fails the contract hash first.
    """
    lines = (PROFILE / "config.yaml").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == "model_routes:")
    indent = len(lines[start]) - len(lines[start].lstrip()) + 2
    aliases: set[str] = set()
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        current = len(line) - len(line.lstrip())
        if current < indent:
            break
        if current == indent:
            aliases.add(line.strip().rstrip(":"))
    return aliases


def _verifier_constant(name: str) -> object:
    source = (PROFILE / "verify_profile.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not defined in verify_profile.py")


def test_the_contract_is_current() -> None:
    # Editing run_api_only.py or config.yaml without regenerating the contract would
    # ship a verifier that checks a hash nothing has any more.
    expected = build_hermes_contract.render(build_hermes_contract.build())
    actual = build_hermes_contract.CONTRACT_PATH.read_text(encoding="utf-8")
    assert actual == expected, "run scripts/build_hermes_contract.py"


def test_the_two_approved_models_are_the_same_two_everywhere() -> None:
    approved = {"glm-5.2": "zai", "MiniMax-M3": "minimax"}
    assert approved == ALLOWED_MODELS
    assert CONTRACT["model_routes"] == approved
    assert _verifier_constant("APPROVED_MODEL_ROUTES") == approved
    assert _config_model_aliases() == set(approved)


def test_the_profile_cannot_open_a_socket_off_this_host() -> None:
    # ADR-0005 §2.2: a unit-level property, not an application setting. Without both
    # lines the profile's only limit would be the proxy it was configured to use.
    unit = _unit("radar-kx-hermes-extraction.service")
    assert "IPAddressDeny=any" in unit
    assert "IPAddressAllow=localhost" in unit
    assert f"Environment=HTTPS_PROXY={CONTRACT['egress']['proxy_url']}" in unit
    assert f"Environment=HTTP_PROXY={CONTRACT['egress']['proxy_url']}" in unit
    assert f"SocketBindAllow=ipv4:tcp:{CONTRACT['api']['port']}" in unit
    assert "SocketBindDeny=any" in unit


def test_the_orchestrator_has_no_internet() -> None:
    # ADR-0005 §6.
    unit = _unit("radar-kx-orchestrator@.service")
    assert "IPAddressDeny=any" in unit
    assert "IPAddressAllow=localhost" in unit


def test_the_orchestrator_instance_name_reaches_the_command_as_arguments() -> None:
    # systemd expands %I without splitting it, so a two-word command passed straight
    # into ExecStart arrives as one argv entry and argparse rejects it. That is how
    # the first production run of this unit failed. An unbraced $VARIABLE is split.
    unit = _unit("radar-kx-orchestrator@.service")
    assert "Environment=RADAR_KX_ORCHESTRATOR_ARGS=%I" in unit
    assert any(
        line.startswith("ExecStart=") and line.endswith("$RADAR_KX_ORCHESTRATOR_ARGS")
        for line in unit
    )
    assert not any(line.startswith("ExecStart=") and line.endswith("%I") for line in unit)


def test_the_proxy_unit_serves_the_port_the_contract_names() -> None:
    unit = _unit("radar-kx-egress-proxy.service")
    port = CONTRACT["egress"]["proxy_url"].rsplit(":", 1)[1]
    assert f"SocketBindAllow=ipv4:tcp:{port}" in unit
    assert any(line.endswith(f"--host 127.0.0.1 --port {port}") for line in unit)
    assert "SocketBindDeny=any" in unit


def test_the_contract_names_the_endpoints_the_proxy_actually_allows() -> None:
    named = {tuple(item.rsplit(":", 1)) for item in CONTRACT["egress"]["allowed_endpoints"]}
    assert {(host, str(port)) for host, port in ALLOWED_ENDPOINTS} == named


def test_the_profile_refuses_to_start_on_a_contract_violation() -> None:
    # 78 is what systemd is told not to restart on. If the verifier ever exited 1 the
    # unit would restart forever against whatever the drifted profile now is.
    assert _verifier_constant("EXIT_CONTRACT_VIOLATION") == 78
    assert "RestartPreventExitStatus=78" in _unit("radar-kx-hermes-extraction.service")
