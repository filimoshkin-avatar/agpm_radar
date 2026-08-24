"""The orchestrator's job at the boundary: refuse, or call and record."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import connect
from radar_kx import orchestrator
from radar_kx.config import Settings
from radar_kx.database import Database
from radar_kx.orchestrator import (
    ALLOWED_MODELS,
    FALLBACK_ORDER,
    REACHABILITY_PROBE,
    RUN_TYPES,
    ModelGateway,
    OrchestratorError,
    RunType,
)


def _settings(dsn: str) -> Settings:
    base = Settings.from_environment()
    return Settings(
        **{
            **{field: getattr(base, field) for field in Settings.__dataclass_fields__},
            "dsn": dsn,
            "release_id": "test-release",
            "hermes_key": "test-key",
            "min_free_bytes": 1024,
            "capacity_path": str(Path(__file__).resolve().parent),
        }
    )


def _reply(content: str = "ready.", *, prompt: int = 11, completion: int = 2) -> Any:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        },
        request=httpx.Request("POST", "http://127.0.0.1:19700/v1/chat/completions"),
    )


def _audit(dsn: str) -> list[dict[str, Any]]:
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT provider, model, purpose, payload_chars, outcome, error_detail,"
            " request_tokens, response_tokens, worker_release"
            " FROM kx.egress_audit ORDER BY egress_id"
        )
        return [dict(row) for row in cursor.fetchall()]


def test_only_the_two_approved_models_exist_in_the_registry() -> None:
    assert set(ALLOWED_MODELS) == {"glm-5.2", "MiniMax-M3"}


def test_every_run_type_declares_a_rule_and_a_cap_that_enforces_it() -> None:
    # ADR-0005 says a run type is not finished until its context rule is written.
    for run_type in RUN_TYPES.values():
        assert run_type.context_rule.strip()
        assert run_type.max_payload_chars > 0
        assert run_type.model in ALLOWED_MODELS


def test_a_successful_call_is_recorded_with_what_crossed(
    migrated_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> Any:
        sent["url"] = url
        sent["json"] = kwargs["json"]
        sent["headers"] = kwargs["headers"]
        return _reply()

    monkeypatch.setattr(httpx, "post", fake_post)
    settings = _settings(migrated_dsn)
    result = ModelGateway(Database(settings), settings).probe()

    assert result.outcome == "succeeded"
    assert result.content == "ready."
    assert sent["url"] == "http://127.0.0.1:19700/v1/chat/completions"
    assert sent["json"]["model"] == "glm-5.2"
    assert sent["headers"]["Authorization"] == "Bearer test-key"

    rows = _audit(migrated_dsn)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "succeeded"
    assert rows[0]["provider"] == "zai"
    assert rows[0]["payload_chars"] == len(orchestrator.PROBE_PROMPT)
    assert rows[0]["request_tokens"] == 11
    assert rows[0]["worker_release"] == "test-release"


def test_a_model_outside_the_two_is_refused_before_anything_leaves(
    migrated_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_to_be_called(url: str, **kwargs: Any) -> Any:
        raise AssertionError("the call must not reach the profile")

    monkeypatch.setattr(httpx, "post", refuse_to_be_called)
    settings = _settings(migrated_dsn)
    with pytest.raises(OrchestratorError, match="not one of"):
        ModelGateway(Database(settings), settings).probe(model="gpt-4o")

    rows = _audit(migrated_dsn)
    assert [row["outcome"] for row in rows] == ["refused_model"]
    assert rows[0]["model"] == "gpt-4o"
    assert rows[0]["provider"] == "unknown"


def test_a_payload_over_the_run_types_cap_is_refused_and_the_refusal_is_recorded(
    migrated_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Context minimization (P18) is a number the code checks, not only a paragraph.
    def refuse_to_be_called(url: str, **kwargs: Any) -> Any:
        raise AssertionError("the call must not reach the profile")

    monkeypatch.setattr(httpx, "post", refuse_to_be_called)
    tiny = RunType(
        name="tiny",
        purpose="test",
        model="glm-5.2",
        context_rule="ten characters, no more",
        max_payload_chars=10,
    )
    settings = _settings(migrated_dsn)
    with pytest.raises(OrchestratorError, match="exceeds the 10"):
        ModelGateway(Database(settings), settings).run(tiny, "x" * 4000)

    rows = _audit(migrated_dsn)
    assert [row["outcome"] for row in rows] == ["refused_oversize_payload"]
    assert rows[0]["payload_chars"] == 4000
    assert "ten characters" in (rows[0]["error_detail"] or "")


def test_a_failed_call_is_recorded_too(migrated_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(url: str, **kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fail)
    settings = _settings(migrated_dsn)
    with pytest.raises(OrchestratorError, match="ConnectError"):
        ModelGateway(Database(settings), settings).probe()

    rows = _audit(migrated_dsn)
    # Every attempt, not one. A refused connection is not a busy profile, so each
    # model is tried once per round and the chain is walked CHAIN_ROUNDS times:
    # two models, two rounds, plus the row written when the call is given up.
    assert [row["outcome"] for row in rows] == ["failed"] * 5
    assert {str(row["model"]) for row in rows} == set(FALLBACK_ORDER)
    assert "connection refused" in (rows[0]["error_detail"] or "")


def test_the_audit_table_refuses_to_be_edited(
    migrated_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: _reply())
    settings = _settings(migrated_dsn)
    ModelGateway(Database(settings), settings).probe()
    with (
        connect(migrated_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(Exception, match="immutable|reject"),
    ):
        cursor.execute("UPDATE kx.egress_audit SET outcome = 'edited'")


# ---------------------------------------------------------------------------
# A busy profile is not a failed batch
# ---------------------------------------------------------------------------


def test_a_busy_profile_is_waited_out_rather_than_dropped() -> None:
    """The profile refuses past ten concurrent runs; two passes cross that line.

    Seventeen of twenty link batches were lost to it before this existed, and a
    lost batch looks exactly like a batch of work nobody had to do.
    """
    from radar_kx.orchestrator import _is_busy

    assert _is_busy('hermes returned 429: {"code": "rate_limit_exceeded"}')
    assert _is_busy("OrchestratorError: Too many concurrent runs (max 10)")
    assert _is_busy("ReadTimeout: timed out")
    # A real refusal is not a busy signal and must not be retried into silence.
    assert not _is_busy("hermes returned 400: model not routed")
    assert not _is_busy("'gpt-9' is not one of ['MiniMax-M3', 'glm-5.2']")


def test_the_retry_budget_is_small_enough_to_surface_a_real_outage() -> None:
    from radar_kx.orchestrator import BUSY_BACKOFF_SECONDS, BUSY_RETRIES

    assert 1 <= BUSY_RETRIES <= 5
    assert BUSY_RETRIES * BUSY_BACKOFF_SECONDS * (BUSY_RETRIES + 1) / 2 < 60


# ---------------------------------------------------------------------------
# When the first model cannot answer
# ---------------------------------------------------------------------------


def test_the_second_model_answers_when_the_first_refuses(migrated_dsn: str) -> None:
    """Both go through one endpoint with one key, so the fallback is free.

    It matters most where nobody is watching: a batch an operator starts fails
    visibly and is started again, a scheduled one fails into a journal.
    """
    gateway = ModelGateway(Database(_settings(migrated_dsn)), _settings(migrated_dsn))
    asked: list[str] = []

    def refuse_the_first(
        model: str, payload: str, system: str | None, **_: object
    ) -> dict[str, object]:
        asked.append(model)
        if model == FALLBACK_ORDER[0]:
            raise OrchestratorError("hermes returned 503: upstream unavailable")
        return {"choices": [{"message": {"content": "ready"}}]}

    gateway._post = refuse_the_first  # type: ignore[method-assign]
    result = gateway.run(REACHABILITY_PROBE, "проверка")

    assert result.outcome == "succeeded"
    assert asked == list(FALLBACK_ORDER), "the chain did not walk in order"
    assert result.model == FALLBACK_ORDER[1]
    assert result.model != REACHABILITY_PROBE.model, "the run type still names the first"


def test_a_model_named_outright_is_not_swapped(migrated_dsn: str) -> None:
    """A measurement of one model must not quietly become a measurement of another."""
    gateway = ModelGateway(Database(_settings(migrated_dsn)), _settings(migrated_dsn))
    asked: list[str] = []

    def always_refuse(
        model: str, payload: str, system: str | None, **_: object
    ) -> dict[str, object]:
        asked.append(model)
        raise OrchestratorError("hermes returned 503: upstream unavailable")

    gateway._post = always_refuse  # type: ignore[method-assign]
    with pytest.raises(OrchestratorError):
        gateway.run(REACHABILITY_PROBE, "проверка", model=FALLBACK_ORDER[0])

    assert set(asked) == {FALLBACK_ORDER[0]}, "an explicit choice was overridden"


def test_every_attempt_is_audited_even_the_ones_that_failed(migrated_dsn: str) -> None:
    """A call that was refused and one that was never made are different facts."""
    settings = _settings(migrated_dsn)
    database = Database(settings)
    gateway = ModelGateway(database, settings)

    def refuse_the_first(
        model: str, payload: str, system: str | None, **_: object
    ) -> dict[str, object]:
        if model == FALLBACK_ORDER[0]:
            raise OrchestratorError("hermes returned 503: upstream unavailable")
        return {"choices": [{"message": {"content": "ready"}}]}

    gateway._post = refuse_the_first  # type: ignore[method-assign]
    gateway.run(REACHABILITY_PROBE, "проверка")

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT model, outcome FROM kx.egress_audit ORDER BY egress_id")
        rows = [(str(row["model"]), str(row["outcome"])) for row in cursor.fetchall()]
    assert (FALLBACK_ORDER[0], "failed") in rows, "the refusal left no record"
    assert (FALLBACK_ORDER[1], "succeeded") in rows, "the answer is not attributed"
