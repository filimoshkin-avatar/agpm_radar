"""Closed Stage 5 candidate builders and dependency-free runtime validation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlsplit

from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.storage.safe_files import SafeFilesystemError, relative_parts

CONTRACT_VERSION: Final = "1.0.0"
SCHEMA_VERSION: Final = 1
_ID: Final = re.compile(r"^[a-z0-9][a-z0-9._:-]{7,127}$")
_ACTOR_ID: Final = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256: Final = re.compile(r"^[a-f0-9]{64}$")
_ERROR_CODE: Final = re.compile(r"^[A-Z0-9_]{3,80}$")
_DATE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_TIMESTAMP: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_PERIOD: Final = re.compile(r"^[0-9]{4}-[0-9]{2}$")
_RUBRIC: Final = re.compile(r"^[a-z0-9._-]{1,80}$")
_FALLBACK_IMPLEMENTATION: Final = re.compile(r"^[a-z0-9._-]{3,100}$")
_FALLBACK_VERSION: Final = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_SQL_PAYLOAD: Final = re.compile(
    r"(?is)\b(?:drop|alter|create)\s+(?:table|index|view|trigger)\b"
    r"|\binsert\s+into\b|\bdelete\s+from\b"
    r"|\bupdate\s+[A-Za-z_][A-Za-z0-9_]*\s+set\b"
    r"|\bpragma\s+[A-Za-z_]|\battach\s+database\b"
)
_HOST_PATH: Final = re.compile(
    r"(?i)(?:^|[\s'\"])(?:/(?:root|mnt|etc|srv|opt|var)(?:/|\b)|[A-Z]:\\|file://)"
)
_SECRET: Final = (
    re.compile(r"-----BEGIN (?:EC |OPENSSH |RSA )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
)


class CandidateValidationError(ValueError):
    """A candidate is ambiguous, malformed, unsafe or outside contract v1."""


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CandidateValidationError(f"{label} must be an object with string keys")
    return cast(dict[str, object], value)


def _exact(value: object, keys: set[str], label: str) -> dict[str, object]:
    result = _object(value, label)
    if set(result) != keys:
        raise CandidateValidationError(f"{label} has unknown or missing fields")
    return result


def _text(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = 20_000,
) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise CandidateValidationError(f"{label} must be text of length {minimum}..{maximum}")
    _scan_text(value, label)
    return value


def _optional_text(value: object, label: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, label, maximum=maximum)


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CandidateValidationError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise CandidateValidationError(f"{label} must be at least {minimum}")
    return value


def _scan_text(value: str, label: str) -> None:
    hidden_state_name = ".open" + "claw"
    if _SQL_PAYLOAD.search(value):
        raise CandidateValidationError(f"SQL/DDL payload is forbidden at {label}")
    if _HOST_PATH.search(value) or hidden_state_name in value.lower():
        raise CandidateValidationError(f"host-local path is forbidden at {label}")
    if any(pattern.search(value) for pattern in _SECRET):
        raise CandidateValidationError(f"secret-shaped content is forbidden at {label}")


def validate_safe_text(value: str, label: str) -> None:
    """Reject executable, host-local or secret-shaped text crossing a package boundary."""
    _scan_text(value, label)


def _id(value: object, label: str) -> str:
    text = _text(value, label, minimum=8, maximum=128)
    if _ID.fullmatch(text) is None:
        raise CandidateValidationError(f"{label} is not a contract identifier")
    return text


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CandidateValidationError(f"{label} must be lowercase SHA-256")
    return value


def _date(value: object, label: str) -> str:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise CandidateValidationError(f"{label} must be an ISO date")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise CandidateValidationError(f"{label} is not a real date") from error
    return value


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise CandidateValidationError(f"{label} must be second-precision UTC")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise CandidateValidationError(f"{label} is not a real UTC timestamp") from error
    return value


def _http_uri(value: object, label: str) -> str:
    text = _text(value, label, minimum=1, maximum=8_000)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        raise CandidateValidationError(f"{label} must be an absolute HTTP(S) URL without userinfo")
    return text


def _relative_path(value: str, label: str) -> None:
    try:
        relative_parts(value)
    except SafeFilesystemError as error:
        raise CandidateValidationError(f"unsafe relative path at {label}: {value!r}") from error


def _provider_model(value: object, label: str) -> tuple[str, str]:
    item = _exact(value, {"provider", "model"}, label)
    return (
        _text(item["provider"], f"{label}.provider", minimum=1, maximum=128),
        _text(item["model"], f"{label}.model", minimum=1, maximum=256),
    )


def validate_llm_outcome(value: object, *, allow_not_requested: bool = True) -> JsonObject:
    """Validate LLM attempt ordering and accepted/effective relationships at runtime."""
    outcome = _exact(
        value,
        {
            "status",
            "requested",
            "attempts",
            "effectiveAttemptOrder",
            "effective",
            "deterministicFallback",
        },
        "llmOutcome",
    )
    status = outcome["status"]
    if status not in {"success", "fallback", "unavailable", "not_requested"}:
        raise CandidateValidationError("llmOutcome.status is invalid")
    if status == "not_requested" and not allow_not_requested:
        raise CandidateValidationError("content candidate must record an attempted/unavailable LLM")
    requested = (
        None
        if outcome["requested"] is None
        else _provider_model(outcome["requested"], "llmOutcome.requested")
    )
    raw_attempts = outcome["attempts"]
    if not isinstance(raw_attempts, list) or len(raw_attempts) > 20:
        raise CandidateValidationError("llmOutcome.attempts must be an array with at most 20 items")
    attempts: dict[int, tuple[str, str, str, bool]] = {}
    for index, raw_attempt in enumerate(raw_attempts, start=1):
        attempt = _exact(
            raw_attempt,
            {"order", "provider", "model", "status", "accepted", "errorCode"},
            f"llmOutcome.attempts[{index - 1}]",
        )
        order = _integer(attempt["order"], "llm attempt order", minimum=1)
        if order != index:
            raise CandidateValidationError("LLM attempt orders must be contiguous and ordered")
        provider = _text(attempt["provider"], "LLM attempt provider", minimum=1, maximum=128)
        model = _text(attempt["model"], "LLM attempt model", minimum=1, maximum=256)
        attempt_status = attempt["status"]
        if attempt_status not in {"success", "error", "invalid", "skipped"}:
            raise CandidateValidationError("LLM attempt status is invalid")
        accepted = attempt["accepted"]
        if not isinstance(accepted, bool):
            raise CandidateValidationError("LLM attempt accepted must be boolean")
        error_code = attempt["errorCode"]
        if error_code is not None and (
            not isinstance(error_code, str) or _ERROR_CODE.fullmatch(error_code) is None
        ):
            raise CandidateValidationError("LLM attempt errorCode is invalid")
        if accepted != (attempt_status == "success"):
            raise CandidateValidationError("only a successful LLM attempt may be accepted")
        if attempt_status == "success" and error_code is not None:
            raise CandidateValidationError("successful LLM attempt cannot carry an error code")
        if attempt_status in {"error", "invalid"} and error_code is None:
            raise CandidateValidationError("failed/invalid LLM attempt requires an error code")
        attempts[order] = (provider, model, cast(str, attempt_status), accepted)

    effective_order = outcome["effectiveAttemptOrder"]
    if effective_order is not None:
        effective_order = _integer(effective_order, "effectiveAttemptOrder", minimum=1)
    effective = (
        None
        if outcome["effective"] is None
        else _provider_model(outcome["effective"], "llmOutcome.effective")
    )
    fallback = outcome["deterministicFallback"]
    if fallback is not None:
        fallback_object = _exact(fallback, {"implementation", "version"}, "deterministicFallback")
        implementation = fallback_object["implementation"]
        version = fallback_object["version"]
        if (
            not isinstance(implementation, str)
            or _FALLBACK_IMPLEMENTATION.fullmatch(implementation) is None
        ):
            raise CandidateValidationError("deterministic fallback implementation is invalid")
        if not isinstance(version, str) or _FALLBACK_VERSION.fullmatch(version) is None:
            raise CandidateValidationError("deterministic fallback version is invalid")

    if status in {"success", "fallback"}:
        if (
            requested is None
            or effective is None
            or effective_order is None
            or fallback is not None
        ):
            raise CandidateValidationError("accepted LLM outcome has an inconsistent shape")
        selected = attempts.get(effective_order)
        if selected is None or not selected[3] or selected[:2] != effective:
            raise CandidateValidationError("effective LLM does not match the accepted attempt")
        if sum(1 for attempt in attempts.values() if attempt[3]) != 1:
            raise CandidateValidationError(
                "accepted LLM outcome must have exactly one accepted attempt"
            )
        if status == "success" and effective != requested:
            raise CandidateValidationError("success must use the requested provider/model")
        if status == "fallback" and effective == requested and effective_order == 1:
            raise CandidateValidationError("fallback must differ from the primary accepted attempt")
    elif status == "unavailable":
        if (
            requested is None
            or effective is not None
            or effective_order is not None
            or fallback is None
        ):
            raise CandidateValidationError(
                "unavailable LLM outcome requires deterministic fallback"
            )
        if any(attempt[3] for attempt in attempts.values()):
            raise CandidateValidationError(
                "unavailable LLM outcome cannot contain an accepted attempt"
            )
    else:
        if (
            requested is not None
            or attempts
            or effective is not None
            or effective_order is not None
        ):
            raise CandidateValidationError(
                "not_requested LLM outcome must not contain model attempts"
            )
        if fallback is not None:
            raise CandidateValidationError("not_requested LLM outcome cannot use fallback")
    return cast(JsonObject, outcome)


def _validate_initiator(value: object) -> None:
    initiator = _exact(value, {"kind", "actorId", "requestId"}, "initiator")
    if initiator["kind"] not in {"project-manager", "owner-request"}:
        raise CandidateValidationError("initiator.kind is invalid")
    actor = initiator["actorId"]
    if not isinstance(actor, str) or _ACTOR_ID.fullmatch(actor) is None:
        raise CandidateValidationError("initiator.actorId is invalid")
    request_id = initiator["requestId"]
    if request_id is not None and (
        not isinstance(request_id, str) or _ACTOR_ID.fullmatch(request_id) is None
    ):
        raise CandidateValidationError("initiator.requestId is invalid")
    if initiator["kind"] == "owner-request" and request_id is None:
        raise CandidateValidationError("owner request must carry requestId")


def _validate_base(value: object) -> None:
    base = _exact(value, {"releaseId", "sequence", "logicalStateHash"}, "expectedBase")
    _id(base["releaseId"], "expectedBase.releaseId")
    _integer(base["sequence"], "expectedBase.sequence", minimum=0)
    _sha256(base["logicalStateHash"], "expectedBase.logicalStateHash")


def _validate_analysis(value: object) -> None:
    analysis = _exact(value, {"headline", "brief", "blocks", "theses"}, "desiredIssue.analysis")
    _optional_text(analysis["headline"], "analysis.headline", maximum=500)
    _optional_text(analysis["brief"], "analysis.brief", maximum=4_000)
    blocks = analysis["blocks"]
    if not isinstance(blocks, list) or len(blocks) > 20:
        raise CandidateValidationError("analysis.blocks must contain at most 20 blocks")
    for index, raw_block in enumerate(blocks):
        block = _exact(raw_block, {"kind", "title", "text"}, f"analysis.blocks[{index}]")
        if block["kind"] not in {"overview", "signals", "risks", "actions"}:
            raise CandidateValidationError("analysis block kind is invalid")
        _text(block["title"], "analysis block title", minimum=1, maximum=300)
        _text(block["text"], "analysis block text", minimum=1, maximum=10_000)
    theses = analysis["theses"]
    if not isinstance(theses, list) or len(theses) > 10:
        raise CandidateValidationError("analysis.theses must contain at most 10 items")
    for index, raw_thesis in enumerate(theses):
        thesis = _exact(raw_thesis, {"lead", "rest"}, f"analysis.theses[{index}]")
        _text(thesis["lead"], "thesis lead", minimum=1, maximum=500)
        _text(thesis["rest"], "thesis rest", maximum=2_000)


def _validate_material(value: object, index: int, global_llm: JsonObject) -> tuple[str, int]:
    keys = {
        "materialId",
        "position",
        "title",
        "url",
        "canonicalUrl",
        "sourceName",
        "publishedAt",
        "publicationDateStatus",
        "perimeter",
        "verdict",
        "summary",
        "agpmTakeaway",
        "brief",
        "keyMaterial",
        "signalScore",
        "signalStrength",
        "theses",
        "trendNotes",
        "flags",
        "rubrics",
        "llmStatus",
        "llmShortText",
        "llmAgpmAngle",
    }
    material = _exact(value, keys, f"desiredIssue.materials[{index}]")
    material_id = _id(material["materialId"], "materialId")
    position = _integer(material["position"], "material position", minimum=1)
    _text(material["title"], "material title", minimum=1, maximum=2_000)
    _http_uri(material["url"], "material URL")
    if material["canonicalUrl"] is not None:
        _http_uri(material["canonicalUrl"], "material canonical URL")
    _optional_text(material["sourceName"], "material sourceName", maximum=500)
    if material["publishedAt"] is not None:
        _timestamp(material["publishedAt"], "material publishedAt")
    if material["publicationDateStatus"] not in {"resolved", "low_confidence", "unresolved"}:
        raise CandidateValidationError("material publicationDateStatus is invalid")
    if material["perimeter"] not in {"near", "mid", "far"}:
        raise CandidateValidationError("material perimeter is invalid")
    if material["verdict"] not in {"core", "adjacent"}:
        raise CandidateValidationError("material verdict is invalid")
    _optional_text(material["summary"], "material summary", maximum=20_000)
    _optional_text(material["agpmTakeaway"], "material agpmTakeaway", maximum=20_000)
    _optional_text(material["brief"], "material brief", maximum=4_000)
    if not isinstance(material["keyMaterial"], bool):
        raise CandidateValidationError("material keyMaterial must be boolean")
    if material["signalScore"] is not None:
        _integer(material["signalScore"], "material signalScore")
    if material["signalStrength"] not in {"strong", "context", "watch"}:
        raise CandidateValidationError("material signalStrength is invalid")
    theses = material["theses"]
    if not isinstance(theses, list) or len(theses) > 20:
        raise CandidateValidationError("material theses must contain at most 20 strings")
    for thesis in theses:
        _text(thesis, "material thesis", maximum=2_000)
    _optional_text(material["trendNotes"], "material trendNotes", maximum=4_000)
    flags = material["flags"]
    allowed_flags = {"governance", "security", "human_in_the_loop", "pmo", "isup", "mcp"}
    if (
        not isinstance(flags, list)
        or any(not isinstance(flag, str) for flag in flags)
        or len(flags) != len(set(cast(list[str], flags)))
    ):
        raise CandidateValidationError("material flags must be a unique array")
    if any(flag not in allowed_flags for flag in flags):
        raise CandidateValidationError("material flag is invalid")
    rubrics = material["rubrics"]
    if (
        not isinstance(rubrics, list)
        or any(not isinstance(rubric, str) for rubric in rubrics)
        or len(rubrics) != len(set(cast(list[str], rubrics)))
    ):
        raise CandidateValidationError("material rubrics must be a unique array")
    if any(not isinstance(rubric, str) or _RUBRIC.fullmatch(rubric) is None for rubric in rubrics):
        raise CandidateValidationError("material rubric id is invalid")
    llm_status = material["llmStatus"]
    if llm_status not in {"success", "fallback", "unavailable"}:
        raise CandidateValidationError("material llmStatus is invalid")
    if llm_status in {"success", "fallback"} and global_llm["effective"] is None:
        raise CandidateValidationError(
            "material accepted LLM output requires a global effective model"
        )
    _optional_text(material["llmShortText"], "material llmShortText", maximum=4_000)
    _optional_text(material["llmAgpmAngle"], "material llmAgpmAngle", maximum=4_000)
    return material_id, position


def _validate_desired_issue(value: object, global_llm: JsonObject) -> dict[str, object]:
    keys = {
        "issueId",
        "issueDate",
        "issueNumber",
        "title",
        "brief",
        "lifecycleStatus",
        "publishedAt",
        "publicationOrigin",
        "emptyReason",
        "materials",
        "analysis",
        "stats",
    }
    issue = _exact(value, keys, "desiredIssue")
    _id(issue["issueId"], "desiredIssue.issueId")
    _date(issue["issueDate"], "desiredIssue.issueDate")
    if issue["issueNumber"] is not None:
        _integer(issue["issueNumber"], "desiredIssue.issueNumber", minimum=1)
    _text(issue["title"], "desiredIssue.title", minimum=1, maximum=1_000)
    _optional_text(issue["brief"], "desiredIssue.brief", maximum=4_000)
    if issue["lifecycleStatus"] != "published":
        raise CandidateValidationError("candidate desired issue must be published")
    if issue["publishedAt"] is not None:
        _timestamp(issue["publishedAt"], "desiredIssue.publishedAt")
    if issue["publicationOrigin"] not in {"v2", "legacy_inferred"}:
        raise CandidateValidationError("desiredIssue.publicationOrigin is invalid")
    if issue["publicationOrigin"] == "v2" and issue["publishedAt"] is None:
        raise CandidateValidationError("V2 publication requires publishedAt")
    _optional_text(issue["emptyReason"], "desiredIssue.emptyReason", maximum=1_000)
    materials = issue["materials"]
    if not isinstance(materials, list) or len(materials) > 100:
        raise CandidateValidationError("desiredIssue.materials must contain at most 100 items")
    identities = [
        _validate_material(item, index, global_llm) for index, item in enumerate(materials)
    ]
    if len({item[0] for item in identities}) != len(identities):
        raise CandidateValidationError("desired issue repeats a material id")
    if sorted(item[1] for item in identities) != list(range(1, len(identities) + 1)):
        raise CandidateValidationError("material positions must be contiguous from 1")
    if bool(materials) == (issue["emptyReason"] is not None):
        raise CandidateValidationError("emptyReason must exist exactly for an empty issue")
    _validate_analysis(issue["analysis"])
    stats = _exact(
        issue["stats"],
        {"viewed", "included", "cut", "near", "mid", "far", "core", "adjacent"},
        "desiredIssue.stats",
    )
    numeric = {key: _integer(value, f"stats.{key}", minimum=0) for key, value in stats.items()}
    if numeric["viewed"] != numeric["included"] + numeric["cut"]:
        raise CandidateValidationError("stats viewed must equal included + cut")
    if numeric["included"] != numeric["near"] + numeric["mid"] + numeric["far"]:
        raise CandidateValidationError("stats included must equal near + mid + far")
    if numeric["included"] != numeric["core"] + numeric["adjacent"]:
        raise CandidateValidationError("stats included must equal core + adjacent")
    if numeric["included"] != len(materials):
        raise CandidateValidationError("stats included must equal the desired material count")
    return issue


def validate_candidate(value: object) -> JsonObject:
    """Validate one daily/correction/gazette object without third-party dependencies."""
    candidate = _object(value, "candidate")
    operation = candidate.get("operation")
    common = {
        "contractVersion",
        "candidateId",
        "idempotencyKey",
        "operation",
        "schemaVersion",
        "createdAt",
        "initiator",
        "reason",
        "expectedBase",
        "llmOutcome",
    }
    operation_keys = {
        "daily": {"snapshot", "expectedIssueAbsent", "desiredIssue", "queueChanges"},
        "correction": {
            "targetIssueDate",
            "expectedIssueStateHash",
            "sharedMaterialPreconditions",
            "desiredIssue",
        },
        "gazette": {
            "gazetteId",
            "expectedGazette",
            "period",
            "title",
            "ownerRequestDigest",
            "htmlEntrypoint",
            "inputAssets",
        },
    }
    if not isinstance(operation, str) or operation not in operation_keys:
        raise CandidateValidationError("candidate operation is invalid")
    candidate = _exact(candidate, common | operation_keys[operation], "candidate")
    schema_version = candidate["schemaVersion"]
    if (
        candidate["contractVersion"] != CONTRACT_VERSION
        or not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        raise CandidateValidationError("candidate contract/schema version mismatch")
    _id(candidate["candidateId"], "candidateId")
    _id(candidate["idempotencyKey"], "idempotencyKey")
    _timestamp(candidate["createdAt"], "createdAt")
    _validate_initiator(candidate["initiator"])
    _text(candidate["reason"], "reason", minimum=3, maximum=2_000)
    _validate_base(candidate["expectedBase"])
    llm = validate_llm_outcome(candidate["llmOutcome"], allow_not_requested=operation == "gazette")

    if operation == "daily":
        snapshot = _exact(
            candidate["snapshot"],
            {"snapshotId", "manifestSha256", "payloadSha256", "itemCount"},
            "snapshot",
        )
        _id(snapshot["snapshotId"], "snapshot.snapshotId")
        _sha256(snapshot["manifestSha256"], "snapshot.manifestSha256")
        _sha256(snapshot["payloadSha256"], "snapshot.payloadSha256")
        _integer(snapshot["itemCount"], "snapshot.itemCount", minimum=0)
        if candidate["expectedIssueAbsent"] is not True:
            raise CandidateValidationError("daily candidate must expect the issue to be absent")
        issue = _validate_desired_issue(candidate["desiredIssue"], llm)
        if issue["publicationOrigin"] != "v2":
            raise CandidateValidationError("daily candidate must create a V2 publication")
        changes = candidate["queueChanges"]
        if not isinstance(changes, list) or len(changes) > 1_000:
            raise CandidateValidationError("queueChanges must contain at most 1000 items")
        queue_ids: set[str] = set()
        for index, raw_change in enumerate(changes):
            change = _exact(
                raw_change,
                {
                    "action",
                    "queueId",
                    "materialId",
                    "state",
                    "targetIssueDate",
                    "priority",
                    "reason",
                },
                f"queueChanges[{index}]",
            )
            if change["action"] not in {"upsert", "delete"}:
                raise CandidateValidationError("queue action is invalid")
            queue_id = _id(change["queueId"], "queueId")
            if queue_id in queue_ids:
                raise CandidateValidationError("queueChanges repeats queueId")
            queue_ids.add(queue_id)
            _id(change["materialId"], "queue materialId")
            if change["state"] not in {"manual", "deferred", "review"}:
                raise CandidateValidationError("queue state is invalid")
            if change["targetIssueDate"] is not None:
                _date(change["targetIssueDate"], "queue targetIssueDate")
            _integer(change["priority"], "queue priority")
            _optional_text(change["reason"], "queue reason", maximum=2_000)
    elif operation == "correction":
        target_date = _date(candidate["targetIssueDate"], "targetIssueDate")
        _sha256(candidate["expectedIssueStateHash"], "expectedIssueStateHash")
        issue = _validate_desired_issue(candidate["desiredIssue"], llm)
        if issue["issueDate"] != target_date:
            raise CandidateValidationError("correction target date differs from desired issue")
        preconditions = candidate["sharedMaterialPreconditions"]
        if not isinstance(preconditions, list) or len(preconditions) > 100:
            raise CandidateValidationError("sharedMaterialPreconditions is invalid")
        material_ids: set[str] = set()
        for index, raw_precondition in enumerate(preconditions):
            precondition = _exact(
                raw_precondition,
                {"materialId", "expectedRowHash"},
                f"sharedMaterialPreconditions[{index}]",
            )
            material_id = _id(precondition["materialId"], "material precondition id")
            if material_id in material_ids:
                raise CandidateValidationError("shared material precondition is duplicated")
            material_ids.add(material_id)
            _sha256(precondition["expectedRowHash"], "material expectedRowHash")
    else:
        _id(candidate["gazetteId"], "gazetteId")
        expected = _object(candidate["expectedGazette"], "expectedGazette")
        if expected.get("state") == "absent":
            _exact(expected, {"state"}, "expectedGazette")
        elif expected.get("state") == "present":
            expected = _exact(expected, {"state", "contentHash"}, "expectedGazette")
            _sha256(expected["contentHash"], "expectedGazette.contentHash")
        else:
            raise CandidateValidationError("expectedGazette state is invalid")
        period = candidate["period"]
        if not isinstance(period, str) or _PERIOD.fullmatch(period) is None:
            raise CandidateValidationError("gazette period is invalid")
        try:
            datetime.strptime(period + "-01", "%Y-%m-%d")
        except ValueError as error:
            raise CandidateValidationError("gazette period is not a real month") from error
        _text(candidate["title"], "gazette title", minimum=1, maximum=1_000)
        _sha256(candidate["ownerRequestDigest"], "ownerRequestDigest")
        entrypoint = _text(candidate["htmlEntrypoint"], "htmlEntrypoint", minimum=1, maximum=512)
        _relative_path(entrypoint, "htmlEntrypoint")
        assets = candidate["inputAssets"]
        if not isinstance(assets, list) or not 1 <= len(assets) <= 1_000:
            raise CandidateValidationError("gazette inputAssets must contain 1..1000 items")
        paths: set[str] = set()
        media_types = {
            "text/html",
            "text/css",
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/svg+xml",
            "font/ttf",
            "font/woff2",
        }
        for index, raw_asset in enumerate(assets):
            asset = _exact(
                raw_asset,
                {"relativePath", "sha256", "bytes", "mediaType"},
                f"inputAssets[{index}]",
            )
            path = _text(asset["relativePath"], "asset relativePath", minimum=1, maximum=512)
            _relative_path(path, f"inputAssets[{index}].relativePath")
            if path in paths:
                raise CandidateValidationError("gazette asset path is duplicated")
            paths.add(path)
            _sha256(asset["sha256"], "asset sha256")
            size = _integer(asset["bytes"], "asset bytes", minimum=1)
            if size > 52_428_800:
                raise CandidateValidationError("gazette asset exceeds 50 MiB")
            if asset["mediaType"] not in media_types:
                raise CandidateValidationError("gazette asset media type is invalid")
        if entrypoint not in paths:
            raise CandidateValidationError("gazette htmlEntrypoint is not an input asset")
        entrypoint_asset = next(
            cast(dict[str, object], asset)
            for asset in assets
            if cast(dict[str, object], asset)["relativePath"] == entrypoint
        )
        if entrypoint_asset["mediaType"] != "text/html":
            raise CandidateValidationError("gazette htmlEntrypoint must be text/html")
    return cast(JsonObject, candidate)


def _build_candidate(
    operation: str, common: Mapping[str, object], extra: Mapping[str, object]
) -> JsonObject:
    candidate = {**common, "operation": operation, **extra}
    return validate_candidate(candidate)


def build_daily_candidate(
    *,
    common: Mapping[str, object],
    snapshot: Mapping[str, object],
    desired_issue: Mapping[str, object],
    queue_changes: Sequence[Mapping[str, object]],
) -> JsonObject:
    """Build and validate one closed daily candidate."""
    return _build_candidate(
        "daily",
        common,
        {
            "desiredIssue": dict(desired_issue),
            "expectedIssueAbsent": True,
            "queueChanges": [dict(change) for change in queue_changes],
            "snapshot": dict(snapshot),
        },
    )


def build_correction_candidate(
    *,
    common: Mapping[str, object],
    target_issue_date: str,
    expected_issue_state_hash: str,
    shared_material_preconditions: Sequence[Mapping[str, object]],
    desired_issue: Mapping[str, object],
) -> JsonObject:
    """Build and validate one closed historical correction candidate."""
    return _build_candidate(
        "correction",
        common,
        {
            "desiredIssue": dict(desired_issue),
            "expectedIssueStateHash": expected_issue_state_hash,
            "sharedMaterialPreconditions": [
                dict(precondition) for precondition in shared_material_preconditions
            ],
            "targetIssueDate": target_issue_date,
        },
    )


def build_gazette_candidate(
    *,
    common: Mapping[str, object],
    gazette: Mapping[str, object],
) -> JsonObject:
    """Build and validate one closed gazette candidate."""
    return _build_candidate("gazette", common, gazette)


def _reject_constant(value: str) -> None:
    raise CandidateValidationError(f"non-finite JSON constant is forbidden: {value}")


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_candidate(path: Path) -> JsonObject:
    """Load a bounded JSON object and reject duplicate keys, NaN and unsafe candidates."""
    return parse_candidate_bytes(path.read_bytes())


def parse_candidate_bytes(content: bytes) -> JsonObject:
    """Parse exact candidate bytes through duplicate-key and runtime contract validation."""
    if len(content) > 16 * 1024 * 1024:
        raise CandidateValidationError("candidate input exceeds 16 MiB")
    try:
        parsed: object = json.loads(
            content,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateValidationError(f"candidate JSON is invalid: {error}") from error
    return validate_candidate(parsed)


def candidate_bytes(candidate: Mapping[str, object]) -> bytes:
    """Return canonical UTF-8 bytes after complete runtime validation."""
    validated = validate_candidate(candidate)
    return canonical_json_line(validated)


__all__ = [
    "CONTRACT_VERSION",
    "SCHEMA_VERSION",
    "CandidateValidationError",
    "build_correction_candidate",
    "build_daily_candidate",
    "build_gazette_candidate",
    "candidate_bytes",
    "load_candidate",
    "parse_candidate_bytes",
    "validate_candidate",
    "validate_llm_outcome",
    "validate_safe_text",
]
