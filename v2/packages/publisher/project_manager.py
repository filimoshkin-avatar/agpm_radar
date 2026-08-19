"""Deterministic Project Manager adapter for authoritative publisher results."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Final, cast

from packages.domain.candidates import (
    CandidateValidationError,
    validate_llm_outcome,
    validate_safe_text,
)
from packages.domain.snapshot import JsonObject, canonical_json_line, sha256_bytes

_ID: Final = re.compile(r"^[a-z0-9][a-z0-9._:-]{7,127}$")
_ERROR: Final = re.compile(r"^[A-Z0-9_]{3,80}$")
_CHECK_ID: Final = re.compile(r"^[a-z0-9._-]{3,80}$")
_SHA256: Final = re.compile(r"^[a-f0-9]{64}$")
_STATUSES: Final = {
    "published",
    "already_succeeded",
    "rejected",
    "failed",
    "rolled_back",
    "needs_reconciliation",
}


class ProjectManagerReportError(ValueError):
    """Publisher result/report semantics are contradictory or incomplete."""


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ProjectManagerReportError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _safe_text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ProjectManagerReportError(f"{label} must be text of at most {maximum} characters")
    try:
        validate_safe_text(value, label)
    except CandidateValidationError as error:
        raise ProjectManagerReportError(str(error)) from error
    return value


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ProjectManagerReportError(f"{label} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProjectManagerReportError(f"{label} is not an RFC 3339 timestamp") from error
    if parsed.tzinfo is None:
        raise ProjectManagerReportError(f"{label} must include a timezone")
    return parsed


def _optional_id(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ProjectManagerReportError(f"{label} is not a contract identifier")
    return value


def _optional_sha256(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProjectManagerReportError(f"{label} must be lowercase SHA-256")
    return value


def _validate_publisher_result(value: object) -> JsonObject:
    result = _object(value, "publisher result")
    required = {
        "contractVersion",
        "candidateId",
        "operation",
        "status",
        "publicationSucceeded",
        "exitCode",
        "startedAt",
        "finishedAt",
        "durationMs",
        "llmOutcome",
        "checks",
        "warnings",
        "error",
        "rollback",
        "idempotencyDisposition",
        "publishingBlocked",
    }
    optional = {
        "activeReleaseId",
        "issueDate",
        "productionStateHash",
        "releaseId",
        "sourceStateHash",
    }
    if not required <= set(result) or set(result) - required - optional:
        raise ProjectManagerReportError("publisher result has unknown or missing fields")
    if result["contractVersion"] != "1.0.0":
        raise ProjectManagerReportError("publisher result contract version differs")
    candidate_id = result["candidateId"]
    if not isinstance(candidate_id, str) or _ID.fullmatch(candidate_id) is None:
        raise ProjectManagerReportError("publisher result candidateId is invalid")
    if result["operation"] not in {"daily", "correction", "gazette"}:
        raise ProjectManagerReportError("publisher result operation is invalid")
    status = result["status"]
    if status not in _STATUSES:
        raise ProjectManagerReportError("publisher result status is invalid")
    if not isinstance(result["publicationSucceeded"], bool):
        raise ProjectManagerReportError("publicationSucceeded must be boolean")
    exit_code = result["exitCode"]
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or not 0 <= exit_code <= 255:
        raise ProjectManagerReportError("publisher exitCode is invalid")
    started_at = _timestamp(result["startedAt"], "publisher startedAt")
    finished_at = _timestamp(result["finishedAt"], "publisher finishedAt")
    if finished_at < started_at:
        raise ProjectManagerReportError("publisher finishedAt precedes startedAt")
    duration = result["durationMs"]
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
        raise ProjectManagerReportError("publisher durationMs is invalid")
    if result["idempotencyDisposition"] not in {"executed", "replayed", "resumed"}:
        raise ProjectManagerReportError("publisher idempotencyDisposition is invalid")
    issue_date = result.get("issueDate")
    if issue_date is not None:
        if not isinstance(issue_date, str):
            raise ProjectManagerReportError("publisher issueDate must be an ISO date")
        try:
            datetime.strptime(issue_date, "%Y-%m-%d")
        except ValueError as error:
            raise ProjectManagerReportError("publisher issueDate is not a real date") from error
    release_id = _optional_id(result.get("releaseId"), "publisher releaseId")
    active_release_id = _optional_id(result.get("activeReleaseId"), "publisher activeReleaseId")
    source_state_hash = _optional_sha256(result.get("sourceStateHash"), "publisher sourceStateHash")
    production_state_hash = _optional_sha256(
        result.get("productionStateHash"), "publisher productionStateHash"
    )
    try:
        validate_llm_outcome(result["llmOutcome"])
    except CandidateValidationError as llm_error:
        raise ProjectManagerReportError(
            f"publisher LLM outcome is invalid: {llm_error}"
        ) from llm_error
    warnings = result["warnings"]
    if not isinstance(warnings, list) or len(warnings) > 100:
        raise ProjectManagerReportError("publisher warnings must be an array of at most 100 items")
    for warning in warnings:
        item = _object(warning, "publisher warning")
        if set(item) != {"code", "message", "ownerVisible"}:
            raise ProjectManagerReportError("publisher warning shape differs")
        if not isinstance(item["code"], str) or _ERROR.fullmatch(item["code"]) is None:
            raise ProjectManagerReportError("publisher warning code is invalid")
        _safe_text(item["message"], "publisher warning message", maximum=2_000)
        if not isinstance(item["ownerVisible"], bool):
            raise ProjectManagerReportError("publisher warning ownerVisible is invalid")
    checks = result["checks"]
    if not isinstance(checks, list) or len(checks) > 500:
        raise ProjectManagerReportError("publisher checks must be an array of at most 500 items")
    check_ids: set[str] = set()
    for raw_check in checks:
        check = _object(raw_check, "publisher check")
        if set(check) != {"id", "status", "message"}:
            raise ProjectManagerReportError("publisher check shape differs")
        check_id = check["id"]
        if not isinstance(check_id, str) or _CHECK_ID.fullmatch(check_id) is None:
            raise ProjectManagerReportError("publisher check id is invalid")
        if check_id in check_ids:
            raise ProjectManagerReportError("publisher check id is duplicated")
        check_ids.add(check_id)
        if check["status"] not in {"passed", "failed", "skipped"}:
            raise ProjectManagerReportError("publisher check status is invalid")
        _safe_text(check["message"], "publisher check message", maximum=1_000)
    rollback = _object(result["rollback"], "publisher rollback")
    if set(rollback) != {
        "required",
        "attempted",
        "succeeded",
        "restoredReleaseId",
        "restoredStateHash",
    }:
        raise ProjectManagerReportError("publisher rollback shape differs")
    if not isinstance(rollback["required"], bool) or not isinstance(rollback["attempted"], bool):
        raise ProjectManagerReportError("publisher rollback flags are invalid")
    if rollback["succeeded"] is not None and not isinstance(rollback["succeeded"], bool):
        raise ProjectManagerReportError("publisher rollback succeeded is invalid")
    restored_release_id = _optional_id(rollback["restoredReleaseId"], "publisher restoredReleaseId")
    restored_state_hash = _optional_sha256(
        rollback["restoredStateHash"], "publisher restoredStateHash"
    )
    error_value = result["error"]
    success = status in {"published", "already_succeeded"}
    if success:
        if (
            result["publicationSucceeded"] is not True
            or exit_code != 0
            or error_value is not None
            or result["publishingBlocked"] is not False
            or release_id is None
            or active_release_id is None
            or source_state_hash is None
            or production_state_hash is None
        ):
            raise ProjectManagerReportError("successful publisher result is contradictory")
    else:
        publisher_error = _object(error_value, "publisher error")
        if set(publisher_error) != {
            "code",
            "category",
            "retryable",
            "message",
            "nextAction",
        }:
            raise ProjectManagerReportError("publisher error shape differs")
        if (
            not isinstance(publisher_error["code"], str)
            or _ERROR.fullmatch(publisher_error["code"]) is None
        ):
            raise ProjectManagerReportError("publisher error code is invalid")
        if publisher_error["category"] not in {
            "validation",
            "security",
            "conflict",
            "compatibility",
            "storage",
            "artifact",
            "transport",
            "activation",
            "smoke",
            "rollback",
            "lock",
            "internal",
        } or not isinstance(publisher_error["retryable"], bool):
            raise ProjectManagerReportError("publisher error category/retryable is invalid")
        _safe_text(publisher_error["message"], "publisher error message", maximum=2_000)
        _safe_text(publisher_error["nextAction"], "publisher error nextAction", maximum=2_000)
        if (
            result["publicationSucceeded"] is not False
            or exit_code == 0
            or not isinstance(publisher_error["message"], str)
            or not isinstance(publisher_error["nextAction"], str)
        ):
            raise ProjectManagerReportError("failed publisher result is contradictory")
        if status == "needs_reconciliation" and result["publishingBlocked"] is not True:
            raise ProjectManagerReportError("needs_reconciliation must block publishing")
        if status != "needs_reconciliation" and result["publishingBlocked"] is not False:
            raise ProjectManagerReportError("only needs_reconciliation may block publishing")
    if status == "rolled_back" and not (
        exit_code == 34
        and rollback["required"] is True
        and rollback["attempted"] is True
        and rollback["succeeded"] is True
        and restored_release_id is not None
        and restored_state_hash is not None
    ):
        raise ProjectManagerReportError(
            "rolled_back publisher result has invalid rollback evidence"
        )
    if status == "needs_reconciliation" and (exit_code != 35 or active_release_id is not None):
        raise ProjectManagerReportError("needs_reconciliation publisher result is contradictory")
    if status in {"rejected", "failed"} and exit_code == 0:
        raise ProjectManagerReportError("failed publisher status requires a nonzero exit code")
    return cast(JsonObject, result)


def build_project_manager_report(publisher_result: Mapping[str, object]) -> JsonObject:
    """Preserve machine semantics and make fallback/no-LLM status owner-visible."""
    result = _validate_publisher_result(publisher_result)
    status = cast(str, result["status"])
    success = status in {"published", "already_succeeded"}
    error = cast(dict[str, object] | None, result["error"])
    llm = cast(dict[str, object], result["llmOutcome"])
    owner_warnings = [
        cast(str, warning["message"])
        for warning in cast(list[dict[str, object]], result["warnings"])
        if warning["ownerVisible"] is True
    ]
    if llm["status"] == "fallback" and not any(
        "fallback" in warning.lower() for warning in owner_warnings
    ):
        effective = cast(dict[str, str], llm["effective"])
        owner_warnings.append(
            "Primary LLM was not accepted; fallback "
            f"{effective['provider']}/{effective['model']} was used."
        )
    if llm["status"] == "unavailable" and not any(
        "llm" in warning.lower() or "model" in warning.lower() for warning in owner_warnings
    ):
        owner_warnings.append("All LLM output was unavailable; deterministic fallback was used.")
    candidate_id = cast(str, result["candidateId"])
    operation = cast(str, result["operation"])
    release_id = cast(str | None, result.get("releaseId"))
    if success:
        disposition = "already completed" if status == "already_succeeded" else "published"
        summary = f"Radar V2 {operation} candidate {candidate_id} {disposition} as {release_id}."
        if llm["status"] == "fallback":
            summary += " A fallback model produced the accepted content."
        elif llm["status"] == "unavailable":
            summary += " Deterministic non-LLM fallback was published."
        error_code: str | None = None
        next_action: str | None = None
    else:
        if error is None:
            raise ProjectManagerReportError("failed publisher result lost its error")
        summary = (
            f"Radar V2 {operation} candidate {candidate_id} ended as {status}: {error['message']}"
        )
        error_code = cast(str, error["code"])
        next_action = cast(str, error["nextAction"])
    raw_report: dict[str, object] = {
        "candidateId": candidate_id,
        "contractVersion": "1.0.0",
        "deliveryRequired": True,
        "errorCode": error_code,
        "issueDate": cast(str | None, result.get("issueDate")),
        "llmOutcome": cast(JsonObject, llm),
        "nextAction": next_action,
        "operation": operation,
        "publicationStatus": status,
        "publicationSucceeded": success,
        "publisherResultSha256": sha256_bytes(canonical_json_line(result)),
        "releaseId": release_id,
        "summary": summary,
        "warnings": owner_warnings,
    }
    report = cast(JsonObject, raw_report)
    if not 10 <= len(summary) <= 4_000:
        raise ProjectManagerReportError("Project Manager summary length is invalid")
    return report


def project_manager_report_bytes(publisher_result: Mapping[str, object]) -> bytes:
    """Return a canonical final delivery payload."""
    return canonical_json_line(build_project_manager_report(publisher_result))


__all__ = [
    "ProjectManagerReportError",
    "build_project_manager_report",
    "project_manager_report_bytes",
]
