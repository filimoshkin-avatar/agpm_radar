"""Derive closed typed row mutations from Stage 5 domain candidates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, cast

from packages.domain.candidates import CandidateValidationError, validate_candidate
from packages.domain.snapshot import JsonObject, SnapshotIdentity, canonical_json_line
from packages.storage.hashing import logical_state_hash
from packages.storage.replication_mutations import (
    TABLE_SPECS,
    TableMutationSpec,
    build_mutation_document,
    row_after_sha256,
)
from packages.storage.sqlite_profile import REQUIRED_SQLITE_PROFILE, assert_sqlite_runtime

_ISSUE_STATE_TABLES: Final = (
    "issues",
    "issue_materials",
    "issue_analysis",
    "material_analysis",
    "material_quality",
    "material_rubrics",
    "daily_stats",
)
_DELETE_PRIORITY: Final = {
    "material_rubrics": 0,
    "material_quality": 1,
    "material_analysis": 2,
    "issue_materials": 3,
    "gazette_assets": 4,
    "editorial_queue": 5,
}
_UPSERT_PRIORITY: Final = {
    "source_snapshots": 20,
    "sources": 21,
    "materials": 22,
    "material_sources": 23,
    "material_evidence": 24,
    "issues": 30,
    "issue_materials": 31,
    "issue_analysis": 32,
    "material_analysis": 33,
    "llm_attempts": 34,
    "material_quality": 35,
    "material_rubrics": 36,
    "daily_stats": 37,
    "editorial_queue": 40,
    "source_rules": 41,
    "gazettes": 50,
    "gazette_assets": 51,
}
_DAILY_CANDIDATE_LOOKBACK_DAYS: Final = 30


class CandidateMutationError(RuntimeError):
    """A valid domain candidate cannot be bound to the current source state."""


@dataclass(frozen=True, slots=True)
class CandidateMutationPlan:
    """Generated typed document and the exact source state it was derived from."""

    document: JsonObject
    source_state_hash: str


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json_line(value)).hexdigest()


def _json_text(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def _row(
    connection: sqlite3.Connection,
    table: str,
    key: Mapping[str, object],
) -> dict[str, object] | None:
    spec = TABLE_SPECS[table]
    where = " AND ".join(f'"{column}" = ?' for column in spec.primary_key)
    columns = ", ".join(f'"{column}"' for column in spec.columns)
    result = connection.execute(
        f'SELECT {columns} FROM "{table}" WHERE {where}',  # noqa: S608
        tuple(key[column] for column in spec.primary_key),
    ).fetchone()
    if result is None:
        return None
    return dict(zip(spec.columns, result, strict=True))


def _rows(
    connection: sqlite3.Connection,
    table: str,
    where: str = "",
    parameters: tuple[object, ...] = (),
) -> tuple[dict[str, object], ...]:
    spec = TABLE_SPECS[table]
    columns = ", ".join(f'"{column}"' for column in spec.columns)
    order = ", ".join(f'"{column}"' for column in spec.primary_key)
    clause = f" WHERE {where}" if where else ""
    query = f'SELECT {columns} FROM "{table}"{clause} ORDER BY {order}'  # noqa: S608
    return tuple(
        dict(zip(spec.columns, row, strict=True)) for row in connection.execute(query, parameters)
    )


def _key(spec: TableMutationSpec, values: Mapping[str, object]) -> dict[str, object]:
    return {column: values[column] for column in spec.primary_key}


def _identity(table: str, key: Mapping[str, object]) -> tuple[str, tuple[tuple[str, object], ...]]:
    spec = TABLE_SPECS[table]
    return table, tuple((column, key[column]) for column in spec.primary_key)


def _make_mutation(
    connection: sqlite3.Connection,
    *,
    table: str,
    action: str,
    values: Mapping[str, object] | None = None,
    key: Mapping[str, object] | None = None,
) -> JsonObject:
    spec = TABLE_SPECS[table]
    mutation_key = dict(key) if key is not None else _key(spec, cast(Mapping[str, object], values))
    current = _row(connection, table, mutation_key)
    if action == "delete" and current is None:
        raise CandidateMutationError(f"cannot delete absent {table} row")
    if action == "insert" and current is not None and current != values:
        raise CandidateMutationError(f"immutable {table} row already exists with different values")
    expected: JsonObject
    if current is None:
        expected = {"state": "absent"}
    else:
        expected = {"rowSha256": row_after_sha256(current), "state": "present"}
    mutation: JsonObject = {
        "action": action,
        "expectedBefore": expected,
        "key": cast(JsonObject, mutation_key),
        "table": table,
    }
    if action != "delete":
        if values is None:
            raise CandidateMutationError(f"{table} mutation has no values")
        mutation["rowAfterSha256"] = row_after_sha256(values)
        mutation["values"] = cast(JsonObject, dict(values))
    return mutation


def _priority(mutation: Mapping[str, object]) -> int:
    table = cast(str, mutation["table"])
    if mutation["action"] == "delete":
        return _DELETE_PRIORITY.get(table, 10)
    return _UPSERT_PRIORITY.get(table, 45)


class _Planner:
    def __init__(self) -> None:
        self._mutations: dict[tuple[str, tuple[tuple[str, object], ...]], JsonObject] = {}

    def add(self, mutation: JsonObject, *, replace: bool = True) -> None:
        table = cast(str, mutation["table"])
        key = cast(dict[str, object], mutation["key"])
        identity = _identity(table, key)
        if replace or identity not in self._mutations:
            self._mutations[identity] = mutation

    def ordered(self) -> tuple[JsonObject, ...]:
        def order_key(mutation: JsonObject) -> tuple[int, str, bytes]:
            return (
                _priority(mutation),
                cast(str, mutation["table"]),
                canonical_json_line(mutation["key"]),
            )

        ordered = sorted(self._mutations.values(), key=order_key)
        return tuple({"sequence": index, **mutation} for index, mutation in enumerate(ordered, 1))


def _verify_source_contract(connection: sqlite3.Connection, candidate: Mapping[str, object]) -> str:
    assert_sqlite_runtime()
    if int(connection.execute("PRAGMA application_id").fetchone()[0]) != (
        REQUIRED_SQLITE_PROFILE.application_id
    ):
        raise CandidateMutationError("source application_id differs from the contract")
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != (
        REQUIRED_SQLITE_PROFILE.user_version
    ):
        raise CandidateMutationError("source schema version differs from the contract")
    violations = tuple(connection.execute("PRAGMA foreign_key_check"))
    if violations:
        raise CandidateMutationError(f"source foreign keys are invalid: {violations!r}")
    compatibility = connection.execute(
        """
        SELECT schema_version, candidate_contract_version
        FROM application_compatibility
        ORDER BY activated_at DESC, application_release_id DESC
        LIMIT 1
        """
    ).fetchone()
    if compatibility is None or tuple(compatibility) != (1, "1.0.0"):
        raise CandidateMutationError("active application is incompatible with candidate v1")
    latest = connection.execute(
        "SELECT release_id, sequence FROM content_releases ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        raise CandidateMutationError("source has no base content release")
    expected_base = cast(dict[str, object], candidate["expectedBase"])
    if tuple(latest) != (expected_base["releaseId"], expected_base["sequence"]):
        raise CandidateMutationError("expected base release/sequence differs from source")
    state_hash = logical_state_hash(connection)
    if expected_base["logicalStateHash"] != state_hash:
        raise CandidateMutationError("expected base logical state hash differs from source")
    duplicate = connection.execute(
        "SELECT 1 FROM content_releases WHERE candidate_id = ?",
        (candidate["candidateId"],),
    ).fetchone()
    if duplicate is not None:
        raise CandidateMutationError("candidate id already exists in the content release ledger")
    return state_hash


def issue_state_hash(connection: sqlite3.Connection, issue_id: str) -> str:
    """Hash the exact mutable issue aggregate used by correction preconditions."""
    if _row(connection, "issues", {"issue_id": issue_id}) is None:
        raise CandidateMutationError(f"issue does not exist: {issue_id}")
    digest = hashlib.sha256(b"radar-v2-issue-state/v1\0")
    for table in _ISSUE_STATE_TABLES:
        for row in _rows(connection, table, '"issue_id" = ?', (issue_id,)):
            digest.update(canonical_json_line({"row": row, "table": table}))
    return digest.hexdigest()


def _source_id(name: str) -> str:
    return "source_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]


def _attempt_id(candidate_id: str, order: int) -> str:
    seed = f"{candidate_id}\0issue\0{order}".encode()
    return "attempt_" + hashlib.sha256(seed).hexdigest()[:24]


def _add_issue_desired_state(
    planner: _Planner,
    connection: sqlite3.Connection,
    candidate: Mapping[str, object],
) -> None:
    issue = cast(dict[str, object], candidate["desiredIssue"])
    issue_id = cast(str, issue["issueId"])
    created_at = cast(str, candidate["createdAt"])
    prior_issue = _row(connection, "issues", {"issue_id": issue_id})
    issue_values: dict[str, object] = {
        "issue_id": issue_id,
        "issue_date": issue["issueDate"],
        "issue_number": issue["issueNumber"],
        "title": issue["title"],
        "brief": issue["brief"],
        "lifecycle_status": issue["lifecycleStatus"],
        "published_at": issue["publishedAt"],
        "publication_origin": issue["publicationOrigin"],
        "empty_reason": issue["emptyReason"],
        "content_hash": _canonical_hash(issue),
        "created_at": prior_issue["created_at"] if prior_issue is not None else created_at,
        "updated_at": created_at,
    }

    desired_materials = cast(list[dict[str, object]], issue["materials"])
    desired_ids = {cast(str, material["materialId"]) for material in desired_materials}
    if candidate["operation"] == "correction":
        existing_links = _rows(connection, "issue_materials", '"issue_id" = ?', (issue_id,))
        for link in existing_links:
            material_id = cast(str, link["material_id"])
            if material_id in desired_ids:
                continue
            link_key = {"issue_id": issue_id, "material_id": material_id}
            for rubric_row in _rows(
                connection,
                "material_rubrics",
                '"issue_id" = ? AND "material_id" = ?',
                (issue_id, material_id),
            ):
                planner.add(
                    _make_mutation(
                        connection,
                        table="material_rubrics",
                        action="delete",
                        key=_key(TABLE_SPECS["material_rubrics"], rubric_row),
                    )
                )
            for table in ("material_quality", "material_analysis"):
                current = _row(connection, table, link_key)
                if current is not None:
                    planner.add(
                        _make_mutation(connection, table=table, action="delete", key=link_key)
                    )
            planner.add(
                _make_mutation(
                    connection,
                    table="issue_materials",
                    action="delete",
                    key=link_key,
                )
            )

    for material in desired_materials:
        material_id = cast(str, material["materialId"])
        source_name = cast(str | None, material["sourceName"])
        prior_material = _row(connection, "materials", {"material_id": material_id})
        material_values: dict[str, object] = {
            "material_id": material_id,
            "title": material["title"],
            "url": material["url"],
            "canonical_url": material["canonicalUrl"],
            "source_name": source_name,
            "published_at": material["publishedAt"],
            "publication_date_status": material["publicationDateStatus"],
            "summary": material["summary"],
            "agpm_takeaway": material["agpmTakeaway"],
            "brief": material["brief"],
            "content_hash": _canonical_hash(
                {
                    key: material[key]
                    for key in (
                        "materialId",
                        "title",
                        "url",
                        "canonicalUrl",
                        "sourceName",
                        "publishedAt",
                        "publicationDateStatus",
                        "summary",
                        "agpmTakeaway",
                        "brief",
                    )
                }
            ),
            "created_at": (
                prior_material["created_at"] if prior_material is not None else created_at
            ),
            "updated_at": created_at,
        }
        planner.add(
            _make_mutation(
                connection,
                table="materials",
                action="upsert",
                values=material_values,
            )
        )
        if source_name:
            source_id = _source_id(source_name)
            source_values = {
                "source_id": source_id,
                "name": source_name,
                "url": None,
                "source_type": "candidate",
                "enabled": 1,
                "updated_at": created_at,
            }
            planner.add(
                _make_mutation(
                    connection,
                    table="sources",
                    action="upsert",
                    values=source_values,
                )
            )
            source_key = {"material_id": material_id, "source_id": source_id}
            prior_source = _row(connection, "material_sources", source_key)
            material_source_values = {
                **source_key,
                "source_url": material["url"],
                "provider": "candidate",
                "first_seen_at": (
                    prior_source["first_seen_at"] if prior_source is not None else created_at
                ),
                "last_seen_at": created_at,
            }
            planner.add(
                _make_mutation(
                    connection,
                    table="material_sources",
                    action="upsert",
                    values=material_source_values,
                )
            )

    planner.add(_make_mutation(connection, table="issues", action="upsert", values=issue_values))

    llm = cast(dict[str, object], candidate["llmOutcome"])
    requested = cast(dict[str, str] | None, llm["requested"])
    effective = cast(dict[str, str] | None, llm["effective"])
    analysis = cast(dict[str, object], issue["analysis"])
    persisted_analysis = {"blocks": analysis["blocks"]}
    for key in ("evidenceTitles", "evidenceMaterialIds", "inputContentHash"):
        if key in analysis:
            persisted_analysis[key] = analysis[key]
    issue_analysis_values = {
        "issue_id": issue_id,
        "headline": analysis["headline"],
        "analysis_json": _json_text(persisted_analysis),
        "theses_json": _json_text(analysis["theses"]),
        "brief": analysis["brief"],
        "llm_status": llm["status"],
        "requested_model": requested["model"] if requested else None,
        "effective_model": effective["model"] if effective else None,
        "provider": effective["provider"] if effective else None,
        "prompt_version": "candidate-v1",
        "updated_at": created_at,
    }
    planner.add(
        _make_mutation(
            connection,
            table="issue_analysis",
            action="upsert",
            values=issue_analysis_values,
        )
    )

    for material in desired_materials:
        material_id = cast(str, material["materialId"])
        link_key = {"issue_id": issue_id, "material_id": material_id}
        prior_link = _row(connection, "issue_materials", link_key)
        link_values = {
            **link_key,
            "sort_order": cast(int, material["position"]) - 1,
            "perimeter": material["perimeter"],
            "verdict": material["verdict"],
            "summary": material["summary"],
            "agpm_takeaway": material["agpmTakeaway"],
            "brief": material["brief"],
            "theses_json": _json_text(material["theses"]),
            "trend_notes": material["trendNotes"],
            "flags_json": _json_text(material["flags"]),
            "key_material": 1 if material["keyMaterial"] else 0,
            "signal_score": material["signalScore"],
            "signal_strength": material["signalStrength"],
            "created_at": prior_link["created_at"] if prior_link is not None else created_at,
            "updated_at": created_at,
        }
        planner.add(
            _make_mutation(
                connection,
                table="issue_materials",
                action="upsert",
                values=link_values,
            )
        )
        material_status = cast(str, material["llmStatus"])
        material_analysis_values = {
            **link_key,
            "short_text": material["llmShortText"],
            "agpm_angle": material["llmAgpmAngle"],
            "llm_status": material_status,
            "requested_model": requested["model"] if requested else None,
            "effective_model": effective["model"]
            if effective and material_status != "unavailable"
            else None,
            "provider": effective["provider"]
            if effective and material_status != "unavailable"
            else None,
            "prompt_version": "candidate-v1",
            "updated_at": created_at,
        }
        planner.add(
            _make_mutation(
                connection,
                table="material_analysis",
                action="upsert",
                values=material_analysis_values,
            )
        )
        issue_date = datetime.strptime(cast(str, issue["issueDate"]), "%Y-%m-%d").date()
        published_at = cast(str | None, material["publishedAt"])
        delta_days = (
            (datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").date() - issue_date).days
            if published_at is not None
            else None
        )
        date_status = cast(str, material["publicationDateStatus"])
        if delta_days is not None and delta_days > 1:
            severity, review_status, reason = (
                "medium",
                "queued",
                "Publication date is after issue window",
            )
        elif date_status == "resolved" and delta_days is not None and abs(delta_days) <= 1:
            severity, review_status, reason = "ok", "ok", None
        elif date_status == "low_confidence":
            severity, review_status, reason = "low", "monitor", "Low-confidence date"
        else:
            severity, review_status, reason = "medium", "queued", "Date requires review"
        quality_values = {
            **link_key,
            "publication_date_status": date_status,
            "issue_date_delta_days": delta_days,
            "severity": severity,
            "review_status": review_status,
            "reason": reason,
            "updated_at": created_at,
        }
        planner.add(
            _make_mutation(
                connection,
                table="material_quality",
                action="upsert",
                values=quality_values,
            )
        )
        desired_rubrics = set(cast(list[str], material["rubrics"]))
        for rubric in desired_rubrics:
            if (
                connection.execute(
                    "SELECT 1 FROM rubrics WHERE rubric_id = ?", (rubric,)
                ).fetchone()
                is None
            ):
                raise CandidateMutationError(f"candidate references unknown rubric: {rubric}")
            rubric_values = {
                **link_key,
                "rubric_id": rubric,
                "confidence": None,
                "source": "candidate",
            }
            planner.add(
                _make_mutation(
                    connection,
                    table="material_rubrics",
                    action="upsert",
                    values=rubric_values,
                )
            )
        for existing in _rows(
            connection,
            "material_rubrics",
            '"issue_id" = ? AND "material_id" = ?',
            (issue_id, material_id),
        ):
            if existing["rubric_id"] not in desired_rubrics:
                planner.add(
                    _make_mutation(
                        connection,
                        table="material_rubrics",
                        action="delete",
                        key=_key(TABLE_SPECS["material_rubrics"], existing),
                    )
                )

    stats = cast(dict[str, int], issue["stats"])
    stats_values = {
        "issue_id": issue_id,
        **stats,
        "updated_at": created_at,
    }
    planner.add(
        _make_mutation(
            connection,
            table="daily_stats",
            action="upsert",
            values=stats_values,
        )
    )
    for raw_attempt in cast(list[dict[str, object]], llm["attempts"]):
        order = cast(int, raw_attempt["order"])
        attempt_values = {
            "attempt_id": _attempt_id(cast(str, candidate["candidateId"]), order),
            "scope": "issue",
            "issue_id": issue_id,
            "material_id": None,
            "requested_model": requested["model"] if requested else None,
            "attempted_model": raw_attempt["model"],
            "provider": raw_attempt["provider"],
            "attempt_order": order,
            "status": raw_attempt["status"],
            "error_code": raw_attempt["errorCode"],
            "started_at": created_at,
            "finished_at": created_at,
        }
        planner.add(
            _make_mutation(
                connection,
                table="llm_attempts",
                action="insert",
                values=attempt_values,
            )
        )


def _add_complete_queue_state(
    planner: _Planner,
    connection: sqlite3.Connection,
    candidate: Mapping[str, object],
) -> None:
    created_at = cast(str, candidate["createdAt"])
    changes = (
        cast(list[dict[str, object]], candidate["queueChanges"])
        if candidate["operation"] == "daily"
        else []
    )
    changed_ids: set[str] = set()
    for change in changes:
        queue_id = cast(str, change["queueId"])
        changed_ids.add(queue_id)
        key = {"queue_id": queue_id}
        current = _row(connection, "editorial_queue", key)
        if change["action"] == "delete":
            if current is None:
                raise CandidateMutationError(f"queue delete targets absent row: {queue_id}")
            expected_fields = {
                "material_id": change["materialId"],
                "state": change["state"],
                "target_issue_date": change["targetIssueDate"],
                "priority": change["priority"],
                "reason": change["reason"],
            }
            if any(current[field] != value for field, value in expected_fields.items()):
                raise CandidateMutationError(f"queue delete precondition differs: {queue_id}")
            planner.add(
                _make_mutation(
                    connection,
                    table="editorial_queue",
                    action="delete",
                    key=key,
                )
            )
        else:
            values = {
                "queue_id": queue_id,
                "material_id": change["materialId"],
                "state": change["state"],
                "target_issue_date": change["targetIssueDate"],
                "priority": change["priority"],
                "reason": change["reason"],
                "created_at": current["created_at"] if current is not None else created_at,
                "updated_at": created_at,
            }
            planner.add(
                _make_mutation(
                    connection,
                    table="editorial_queue",
                    action="upsert",
                    values=values,
                )
            )
    for current in _rows(connection, "editorial_queue"):
        if current["queue_id"] not in changed_ids:
            planner.add(
                _make_mutation(
                    connection,
                    table="editorial_queue",
                    action="upsert",
                    values=current,
                ),
                replace=False,
            )


def _add_draft_state(
    planner: _Planner,
    connection: sqlite3.Connection,
) -> None:
    draft_issues = _rows(connection, "issues", '"lifecycle_status" = ?', ("draft",))
    issue_ids = [cast(str, issue["issue_id"]) for issue in draft_issues]
    material_ids: set[str] = set()
    for issue in draft_issues:
        planner.add(
            _make_mutation(connection, table="issues", action="upsert", values=issue),
            replace=False,
        )
    for issue_id in issue_ids:
        for table in (
            "issue_materials",
            "issue_analysis",
            "material_analysis",
            "material_quality",
            "material_rubrics",
            "daily_stats",
        ):
            for current in _rows(connection, table, '"issue_id" = ?', (issue_id,)):
                if table == "issue_materials":
                    material_ids.add(cast(str, current["material_id"]))
                planner.add(
                    _make_mutation(
                        connection,
                        table=table,
                        action="upsert",
                        values=current,
                    ),
                    replace=False,
                )
        for current in _rows(connection, "llm_attempts", '"issue_id" = ?', (issue_id,)):
            planner.add(
                _make_mutation(
                    connection,
                    table="llm_attempts",
                    action="insert",
                    values=current,
                ),
                replace=False,
            )
    source_ids: set[str] = set()
    for material_id in sorted(material_ids):
        material = _row(connection, "materials", {"material_id": material_id})
        if material is None:
            raise CandidateMutationError(f"draft references absent material: {material_id}")
        planner.add(
            _make_mutation(
                connection,
                table="materials",
                action="upsert",
                values=material,
            ),
            replace=False,
        )
        for table in ("material_sources", "material_evidence"):
            for current in _rows(connection, table, '"material_id" = ?', (material_id,)):
                if table == "material_sources":
                    source_ids.add(cast(str, current["source_id"]))
                planner.add(
                    _make_mutation(
                        connection,
                        table=table,
                        action="upsert",
                        values=current,
                    ),
                    replace=False,
                )
    for source_id in sorted(source_ids):
        source = _row(connection, "sources", {"source_id": source_id})
        if source is None:
            raise CandidateMutationError(f"draft material references absent source: {source_id}")
        planner.add(
            _make_mutation(connection, table="sources", action="upsert", values=source),
            replace=False,
        )


def _validate_daily_preconditions(
    connection: sqlite3.Connection,
    candidate: Mapping[str, object],
    snapshot: SnapshotIdentity,
) -> None:
    candidate_snapshot = cast(dict[str, object], candidate["snapshot"])
    expected = {
        "snapshotId": snapshot.snapshot_id,
        "manifestSha256": snapshot.manifest_sha256,
        "payloadSha256": snapshot.payload_sha256,
        "itemCount": snapshot.item_count,
    }
    if candidate_snapshot != expected:
        raise CandidateMutationError("daily candidate differs from verified V2 snapshot identity")
    issue = cast(dict[str, object], candidate["desiredIssue"])
    conflict = connection.execute(
        "SELECT issue_id FROM issues WHERE issue_id = ? OR issue_date = ?",
        (issue["issueId"], issue["issueDate"]),
    ).fetchone()
    if conflict is not None:
        raise CandidateMutationError("daily candidate issue id/date is already present")
    issue_day = datetime.strptime(cast(str, issue["issueDate"]), "%Y-%m-%d").date()
    earliest_day = issue_day - timedelta(days=_DAILY_CANDIDATE_LOOKBACK_DAYS)
    for material in cast(list[dict[str, object]], issue["materials"]):
        material_id = cast(str, material["materialId"])
        prior_issue = connection.execute(
            "SELECT issue_id FROM issue_materials WHERE material_id = ? LIMIT 1",
            (material_id,),
        ).fetchone()
        if prior_issue is not None:
            raise CandidateMutationError(
                "daily candidate material was already included in an earlier issue"
            )
        published_at = cast(str | None, material["publishedAt"])
        date_status = cast(str, material["publicationDateStatus"])
        if date_status == "unresolved":
            if published_at is not None:
                raise CandidateMutationError(
                    "unresolved daily candidate material must not have publishedAt"
                )
            continue
        if published_at is None:
            raise CandidateMutationError("dated daily candidate material must have publishedAt")
        published_day = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").date()
        if not earliest_day <= published_day <= issue_day:
            raise CandidateMutationError(
                "daily candidate material is outside the 30-day publication window"
            )


def _validate_correction_preconditions(
    connection: sqlite3.Connection,
    candidate: Mapping[str, object],
) -> None:
    issue = cast(dict[str, object], candidate["desiredIssue"])
    existing = connection.execute(
        "SELECT issue_id FROM issues WHERE issue_date = ?",
        (candidate["targetIssueDate"],),
    ).fetchone()
    if existing is None or existing[0] != issue["issueId"]:
        raise CandidateMutationError("correction target does not identify the current issue")
    actual_hash = issue_state_hash(connection, cast(str, issue["issueId"]))
    if candidate["expectedIssueStateHash"] != actual_hash:
        raise CandidateMutationError("correction issue-state precondition differs")
    for precondition in cast(list[dict[str, object]], candidate["sharedMaterialPreconditions"]):
        material = _row(
            connection,
            "materials",
            {"material_id": precondition["materialId"]},
        )
        if material is None or row_after_sha256(material) != precondition["expectedRowHash"]:
            raise CandidateMutationError("shared material precondition differs")


def _add_gazette_state(
    planner: _Planner,
    connection: sqlite3.Connection,
    candidate: Mapping[str, object],
) -> None:
    gazette_id = cast(str, candidate["gazetteId"])
    current = _row(connection, "gazettes", {"gazette_id": gazette_id})
    expected = cast(dict[str, object], candidate["expectedGazette"])
    if expected["state"] == "absent" and current is not None:
        raise CandidateMutationError("gazette was expected absent but is present")
    if expected["state"] == "present" and (
        current is None or current["content_hash"] != expected["contentHash"]
    ):
        raise CandidateMutationError("gazette content-hash precondition differs")
    assets = cast(list[dict[str, object]], candidate["inputAssets"])
    descriptors = [
        {
            "bytes": asset["bytes"],
            "mediaType": asset["mediaType"],
            "relativePath": asset["relativePath"],
            "sha256": asset["sha256"],
        }
        for asset in assets
    ]
    asset_manifest_sha256 = _canonical_hash(descriptors)
    content_hash = _canonical_hash(
        {
            "gazetteId": gazette_id,
            "period": candidate["period"],
            "title": candidate["title"],
            "assets": descriptors,
        }
    )
    created_at = cast(str, candidate["createdAt"])
    values = {
        "gazette_id": gazette_id,
        "period": candidate["period"],
        "title": candidate["title"],
        "lifecycle_status": "published",
        "published_at": created_at,
        "asset_manifest_sha256": asset_manifest_sha256,
        "content_hash": content_hash,
        "created_at": current["created_at"] if current is not None else created_at,
        "updated_at": created_at,
    }
    existing_paths = {
        cast(str, row["relative_path"]): row
        for row in _rows(connection, "gazette_assets", '"gazette_id" = ?', (gazette_id,))
    }
    desired_paths = {cast(str, asset["relativePath"]) for asset in assets}
    for path, existing_asset in existing_paths.items():
        if path not in desired_paths:
            planner.add(
                _make_mutation(
                    connection,
                    table="gazette_assets",
                    action="delete",
                    key=_key(TABLE_SPECS["gazette_assets"], existing_asset),
                )
            )
    planner.add(_make_mutation(connection, table="gazettes", action="upsert", values=values))
    for asset in assets:
        asset_values = {
            "gazette_id": gazette_id,
            "relative_path": asset["relativePath"],
            "sha256": asset["sha256"],
            "bytes": asset["bytes"],
            "media_type": asset["mediaType"],
        }
        planner.add(
            _make_mutation(
                connection,
                table="gazette_assets",
                action="upsert",
                values=asset_values,
            )
        )


def build_candidate_mutations(
    connection: sqlite3.Connection,
    candidate: Mapping[str, object],
    *,
    snapshot_identity: SnapshotIdentity | None = None,
    snapshot_collected_at: str | None = None,
) -> CandidateMutationPlan:
    """Bind a validated candidate to source rows without accepting caller-authored SQL."""
    try:
        validated = validate_candidate(candidate)
    except CandidateValidationError as error:
        raise CandidateMutationError(str(error)) from error
    connection.row_factory = sqlite3.Row
    source_state_hash = _verify_source_contract(connection, validated)
    planner = _Planner()
    operation = validated["operation"]
    if operation == "daily":
        if snapshot_identity is None or snapshot_collected_at is None:
            raise CandidateMutationError("daily candidate requires verified V2 snapshot evidence")
        _validate_daily_preconditions(connection, validated, snapshot_identity)
        snapshot_values = {
            "snapshot_id": snapshot_identity.snapshot_id,
            "manifest_sha256": snapshot_identity.manifest_sha256,
            "payload_sha256": snapshot_identity.payload_sha256,
            "collected_at": snapshot_collected_at,
            "item_count": snapshot_identity.item_count,
        }
        planner.add(
            _make_mutation(
                connection,
                table="source_snapshots",
                action="insert",
                values=snapshot_values,
            )
        )
        _add_issue_desired_state(planner, connection, validated)
        _add_complete_queue_state(planner, connection, validated)
    elif operation == "correction":
        if snapshot_identity is not None or snapshot_collected_at is not None:
            raise CandidateMutationError("correction candidate cannot carry snapshot evidence")
        _validate_correction_preconditions(connection, validated)
        _add_issue_desired_state(planner, connection, validated)
        _add_complete_queue_state(planner, connection, validated)
    else:
        if snapshot_identity is not None or snapshot_collected_at is not None:
            raise CandidateMutationError("gazette candidate cannot carry snapshot evidence")
        _add_gazette_state(planner, connection, validated)
        _add_complete_queue_state(planner, connection, validated)
    _add_draft_state(planner, connection)
    document = build_mutation_document(validated, planner.ordered())
    return CandidateMutationPlan(document=document, source_state_hash=source_state_hash)


__all__ = [
    "CandidateMutationError",
    "CandidateMutationPlan",
    "build_candidate_mutations",
    "issue_state_hash",
]
