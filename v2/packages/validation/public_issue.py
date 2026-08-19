"""Published-only Radar V2 issue validation and explicit public DTO projection."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Final, cast
from urllib.parse import urlsplit, urlunsplit

from packages.domain.snapshot import JsonObject, JsonValue
from packages.storage.sqlite_profile import REQUIRED_SQLITE_PROFILE, assert_sqlite_runtime

_PUBLIC_ISSUE_KEYS: Final = {
    "analysis",
    "brief",
    "issueDate",
    "issueNumber",
    "llm",
    "materialCount",
    "materials",
    "publishedAt",
    "stats",
    "theses",
    "title",
}
_MATERIAL_KEYS: Final = {
    "agpmTakeaway",
    "brief",
    "canonicalUrl",
    "id",
    "issueDate",
    "keyMaterial",
    "llm",
    "perimeter",
    "publicationDateStatus",
    "publishedAt",
    "rubrics",
    "signalScore",
    "signalStrength",
    "sourceName",
    "summary",
    "theses",
    "title",
    "trendNotes",
    "url",
    "verdict",
}
_STATS_KEYS: Final = {
    "adjacent",
    "core",
    "cut",
    "far",
    "included",
    "mid",
    "near",
    "viewed",
}
_BLOCK_KINDS: Final = frozenset({"overview", "signals", "risks", "actions"})
_LLM_STATUSES: Final = frozenset({"success", "fallback", "unavailable"})
_DATE_STATUS: Final = frozenset({"resolved", "low_confidence", "unresolved"})
_PERIMETERS: Final = frozenset({"near", "mid", "far"})
_VERDICTS: Final = frozenset({"core", "adjacent"})
_SIGNAL_STRENGTHS: Final = frozenset({"strong", "context", "watch"})
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


class PublicIssueValidationError(ValueError):
    """A database row set or rendered public issue violates contract v1."""


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PublicIssueValidationError(f"{label} must be an object with string keys")
    return cast(dict[str, object], value)


def _exact(value: object, keys: set[str], label: str) -> dict[str, object]:
    result = _object(value, label)
    if set(result) != keys:
        raise PublicIssueValidationError(f"{label} has unknown or missing fields")
    return result


def _text(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = 20_000,
) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise PublicIssueValidationError(f"{label} must be text of length {minimum}..{maximum}")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise PublicIssueValidationError(f"{label} contains forbidden control characters")
    return value


def _optional_text(value: object, label: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, label, maximum=maximum)


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PublicIssueValidationError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise PublicIssueValidationError(f"{label} must be at least {minimum}")
    return value


def _date(value: object, label: str) -> str:
    text = _text(value, label, minimum=10, maximum=10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise PublicIssueValidationError(f"{label} is not an ISO date") from error
    if parsed.isoformat() != text:
        raise PublicIssueValidationError(f"{label} is not a canonical ISO date")
    return text


def _timestamp(value: object, label: str) -> str:
    text = _text(value, label, minimum=20, maximum=20)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise PublicIssueValidationError(f"{label} is not second-precision UTC") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != text:
        raise PublicIssueValidationError(f"{label} is not canonical UTC")
    return text


def _optional_timestamp(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _timestamp(value, label)


def _database_timestamp(
    value: object,
    label: str,
    *,
    allow_legacy_date: bool,
) -> str | None:
    if value is None:
        return None
    try:
        return _timestamp(value, label)
    except PublicIssueValidationError:
        if allow_legacy_date:
            legacy_date = _date(value, label)
            return f"{legacy_date}T00:00:00Z"
        raise


def _url(value: object, label: str) -> str:
    text = _text(value, label, minimum=1, maximum=8_000)
    parsed = urlsplit(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise PublicIssueValidationError(
            f"{label} must be an absolute HTTP(S) URL without userinfo"
        )
    return text


def _optional_url(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _url(value, label)


def _llm(value: object, label: str) -> dict[str, object]:
    outcome = _exact(value, {"effectiveModel", "status"}, label)
    status = outcome["status"]
    if status not in _LLM_STATUSES:
        raise PublicIssueValidationError(f"{label}.status is invalid")
    effective = _optional_text(outcome["effectiveModel"], f"{label}.effectiveModel", maximum=256)
    if status == "success" and effective is None:
        raise PublicIssueValidationError(f"{label} success requires an effective model")
    if status == "unavailable" and effective is not None:
        raise PublicIssueValidationError(f"{label} unavailable cannot claim an effective model")
    return outcome


def _stats(value: object, label: str = "stats") -> dict[str, object]:
    stats = _exact(value, _STATS_KEYS, label)
    values = {key: _integer(stats[key], f"{label}.{key}", minimum=0) for key in _STATS_KEYS}
    if values["viewed"] != values["included"] + values["cut"]:
        raise PublicIssueValidationError("stats viewed must equal included + cut")
    if values["included"] != values["near"] + values["mid"] + values["far"]:
        raise PublicIssueValidationError("stats included must equal near + mid + far")
    if values["included"] != values["core"] + values["adjacent"]:
        raise PublicIssueValidationError("stats included must equal core + adjacent")
    return stats


def _canonical_url_key(value: str) -> str:
    parsed = urlsplit(value)
    host = cast(str, parsed.hostname).lower()
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))


def _scan_public_value(value: JsonValue, label: str = "public issue") -> None:
    if isinstance(value, str):
        hidden_state_name = ".open" + "claw"
        if _HOST_PATH.search(value) or hidden_state_name in value.lower():
            raise PublicIssueValidationError(f"host-local path leaked at {label}")
        if any(pattern.search(value) for pattern in _SECRET):
            raise PublicIssueValidationError(f"secret-shaped content leaked at {label}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_public_value(item, f"{label}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _scan_public_value(item, f"{label}.{key}")


def validate_public_value(value: JsonValue, *, label: str = "public value") -> None:
    """Reject host-local paths and secret-shaped strings in any public DTO fragment."""
    _scan_public_value(value, label)


def validate_public_issue_document(value: object) -> JsonObject:
    """Validate the exact deterministic IssueDetail projection used by renderers."""
    issue = _exact(value, _PUBLIC_ISSUE_KEYS, "public issue")
    issue_date = _date(issue["issueDate"], "issueDate")
    if issue["issueNumber"] is not None:
        _integer(issue["issueNumber"], "issueNumber", minimum=1)
    _text(issue["title"], "title", minimum=1, maximum=1_000)
    _optional_text(issue["brief"], "brief", maximum=4_000)
    _optional_timestamp(issue["publishedAt"], "publishedAt")
    _llm(issue["llm"], "llm")
    stats = _stats(issue["stats"])

    analysis = _exact(issue["analysis"], {"blocks", "brief", "headline"}, "analysis")
    _optional_text(analysis["headline"], "analysis.headline", maximum=500)
    _optional_text(analysis["brief"], "analysis.brief", maximum=4_000)
    blocks = analysis["blocks"]
    if not isinstance(blocks, list) or not blocks or len(blocks) > 20:
        raise PublicIssueValidationError("analysis.blocks must contain 1..20 items")
    for index, raw_block in enumerate(blocks):
        block = _exact(raw_block, {"kind", "text", "title"}, f"analysis.blocks[{index}]")
        if block["kind"] not in _BLOCK_KINDS:
            raise PublicIssueValidationError("analysis block kind is invalid")
        _text(block["title"], "analysis block title", minimum=1, maximum=300)
        _text(block["text"], "analysis block text", minimum=1, maximum=10_000)

    theses = issue["theses"]
    if not isinstance(theses, list) or len(theses) > 10:
        raise PublicIssueValidationError("theses must contain at most 10 items")
    for index, raw_thesis in enumerate(theses):
        thesis = _exact(raw_thesis, {"lead", "rest"}, f"theses[{index}]")
        _text(thesis["lead"], "thesis lead", minimum=1, maximum=500)
        _text(thesis["rest"], "thesis rest", maximum=2_000)

    materials = issue["materials"]
    if not isinstance(materials, list) or len(materials) > 100:
        raise PublicIssueValidationError("materials must contain at most 100 items")
    material_count = _integer(issue["materialCount"], "materialCount", minimum=0)
    if material_count != len(materials) or material_count != stats["included"]:
        raise PublicIssueValidationError("materialCount, materials and stats.included differ")
    duplicate_keys: set[str] = set()
    material_ids: set[str] = set()
    for index, raw_material in enumerate(materials):
        material = _exact(raw_material, _MATERIAL_KEYS, f"materials[{index}]")
        material_id = _text(material["id"], "material id", minimum=1, maximum=128)
        if material_id in material_ids:
            raise PublicIssueValidationError("material id is duplicated")
        material_ids.add(material_id)
        if _date(material["issueDate"], "material issueDate") != issue_date:
            raise PublicIssueValidationError("material issueDate differs from issueDate")
        _text(material["title"], "material title", minimum=1, maximum=2_000)
        url = _url(material["url"], "material URL")
        canonical_url = _optional_url(material["canonicalUrl"], "material canonical URL")
        duplicate_key = _canonical_url_key(canonical_url or url)
        if duplicate_key in duplicate_keys:
            raise PublicIssueValidationError("duplicate material URL in one issue")
        duplicate_keys.add(duplicate_key)
        _optional_text(material["sourceName"], "material sourceName", maximum=500)
        _optional_timestamp(material["publishedAt"], "material publishedAt")
        if material["publicationDateStatus"] not in _DATE_STATUS:
            raise PublicIssueValidationError("material publicationDateStatus is invalid")
        if material["perimeter"] not in _PERIMETERS:
            raise PublicIssueValidationError("material perimeter is invalid")
        if material["verdict"] not in _VERDICTS:
            raise PublicIssueValidationError("material verdict is invalid")
        _optional_text(material["brief"], "material brief", maximum=4_000)
        _optional_text(material["summary"], "material summary", maximum=20_000)
        _optional_text(material["agpmTakeaway"], "material agpmTakeaway", maximum=20_000)
        if not isinstance(material["keyMaterial"], bool):
            raise PublicIssueValidationError("material keyMaterial must be boolean")
        if material["signalScore"] is not None:
            _integer(material["signalScore"], "material signalScore")
        if material["signalStrength"] not in _SIGNAL_STRENGTHS:
            raise PublicIssueValidationError("material signalStrength is invalid")
        material_theses = material["theses"]
        if not isinstance(material_theses, list) or len(material_theses) > 20:
            raise PublicIssueValidationError("material theses must contain at most 20 items")
        for thesis in material_theses:
            _text(thesis, "material thesis", maximum=2_000)
        _optional_text(material["trendNotes"], "material trendNotes", maximum=4_000)
        rubrics = material["rubrics"]
        if (
            not isinstance(rubrics, list)
            or any(
                not isinstance(rubric, str) or not rubric or len(rubric) > 80 for rubric in rubrics
            )
            or len(cast(list[str], rubrics)) != len(set(cast(list[str], rubrics)))
        ):
            raise PublicIssueValidationError("material rubrics must be unique bounded strings")
        _llm(material["llm"], "material llm")
    _scan_public_value(cast(JsonValue, issue))
    return cast(JsonObject, issue)


def _row(cursor: sqlite3.Cursor, label: str) -> dict[str, object]:
    raw = cursor.fetchone()
    if raw is None or cursor.description is None:
        raise PublicIssueValidationError(f"missing {label}")
    return dict(zip((column[0] for column in cursor.description), raw, strict=True))


def _rows(cursor: sqlite3.Cursor) -> tuple[dict[str, object], ...]:
    if cursor.description is None:
        return ()
    columns = tuple(column[0] for column in cursor.description)
    return tuple(dict(zip(columns, raw, strict=True)) for raw in cursor.fetchall())


def _json(value: object, label: str) -> object:
    if not isinstance(value, str):
        raise PublicIssueValidationError(f"{label} must be JSON text")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise PublicIssueValidationError(f"invalid JSON in {label}") from error


def _fallback_block(material_count: int, stats: Mapping[str, object], status: str) -> JsonObject:
    prefix = (
        "Все LLM-провайдеры недоступны."  # noqa: RUF001
        if status == "unavailable"
        else "Использовано детерминированное fallback-представление."
    )
    if material_count == 0:
        body = " Выпуск не содержит квалифицирующих материалов."
    else:
        body = (
            f" В выпуск включено {material_count} материалов: "  # noqa: RUF001
            f"ближний периметр — {stats['near']}, "
            f"средний — {stats['mid']}, дальний — {stats['far']}."
        )
    return {
        "kind": "overview",
        "text": prefix + body,
        "title": "Детерминированный обзор",
    }


def _analysis_blocks(
    raw: object, material_count: int, stats: Mapping[str, object], status: str
) -> list[JsonValue]:
    value = _object(raw, "issue_analysis.analysis_json")
    direct = value.get("blocks")
    if isinstance(direct, list) and direct:
        return cast(list[JsonValue], direct)
    daily = value.get("daily")
    if isinstance(daily, dict):
        daily_object = cast(dict[str, object], daily)
        mapped: list[JsonValue] = []
        for key, kind, title in (
            ("signal", "signals", "Сигнал выпуска"),
            ("why_agpm", "overview", "Значение для AgPM"),
            ("watch_next", "actions", "Что отслеживать дальше"),
        ):
            text = daily_object.get(key)
            if isinstance(text, str) and text:
                mapped.append({"kind": kind, "text": text, "title": title})
        if mapped:
            return mapped
    return [_fallback_block(material_count, stats, status)]


def _verify_database_boundary(connection: sqlite3.Connection) -> None:
    assert_sqlite_runtime()
    if int(connection.execute("PRAGMA application_id").fetchone()[0]) != (
        REQUIRED_SQLITE_PROFILE.application_id
    ):
        raise PublicIssueValidationError("database application_id differs from contract")
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != (
        REQUIRED_SQLITE_PROFILE.user_version
    ):
        raise PublicIssueValidationError("database schema version differs from contract")
    if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
        raise PublicIssueValidationError("database integrity_check failed")
    violations = tuple(connection.execute("PRAGMA foreign_key_check"))
    if violations:
        raise PublicIssueValidationError(f"database foreign keys are invalid: {violations!r}")
    compatibility = connection.execute(
        """
        SELECT schema_version, candidate_contract_version, public_api_version
        FROM application_compatibility
        ORDER BY activated_at DESC, application_release_id DESC
        LIMIT 1
        """
    ).fetchone()
    if compatibility != (1, "1.0.0", "1.0.0"):
        raise PublicIssueValidationError("active application compatibility differs from V2 v1")


def verify_public_database_connection(connection: sqlite3.Connection) -> None:
    """Verify the database/profile boundary before installing a read-only API authorizer."""
    _verify_database_boundary(connection)


def build_public_issue(
    connection: sqlite3.Connection,
    *,
    issue_date: str,
) -> JsonObject:
    """Validate one published aggregate and build its explicit IssueDetail DTO."""
    _verify_database_boundary(connection)
    requested_date = _date(issue_date, "requested issue date")
    issue = _row(
        connection.execute(
            """
            SELECT issue_id, issue_date, issue_number, title, brief, lifecycle_status,
                   published_at, publication_origin, empty_reason
            FROM issues
            WHERE issue_date = ?
            """,
            (requested_date,),
        ),
        "issue",
    )
    if issue["lifecycle_status"] != "published":
        raise PublicIssueValidationError("requested issue is not published")
    legacy_inferred = issue["publication_origin"] == "legacy_inferred"
    issue_published_at = _database_timestamp(
        issue["published_at"],
        "database issue published_at",
        allow_legacy_date=legacy_inferred,
    )
    if issue["publication_origin"] == "v2" and issue_published_at is None:
        raise PublicIssueValidationError("V2 published issue has no published_at")
    issue_id = cast(str, issue["issue_id"])
    stats_row = _row(
        connection.execute(
            """
            SELECT viewed, included, cut, near, mid, far, core, adjacent
            FROM daily_stats WHERE issue_id = ?
            """,
            (issue_id,),
        ),
        "daily stats",
    )
    stats = _stats(stats_row, "database stats")
    analysis_row = _row(
        connection.execute(
            """
            SELECT headline, analysis_json, theses_json, brief, llm_status,
                   effective_model
            FROM issue_analysis WHERE issue_id = ?
            """,
            (issue_id,),
        ),
        "issue analysis",
    )
    issue_llm = {
        "effectiveModel": analysis_row["effective_model"],
        "status": analysis_row["llm_status"],
    }
    _llm(issue_llm, "database issue llm")

    material_rows = _rows(
        connection.execute(
            """
            SELECT im.sort_order, im.perimeter, im.verdict, im.summary,
                   im.agpm_takeaway, im.brief, im.theses_json, im.trend_notes,
                   im.key_material, im.signal_score, im.signal_strength,
                   m.material_id, m.title, m.url, m.canonical_url, m.source_name,
                   m.published_at, m.publication_date_status,
                   ma.llm_status, ma.effective_model,
                   mq.publication_date_status AS quality_date_status,
                   mq.issue_date_delta_days, mq.severity, mq.review_status
            FROM issue_materials AS im
            JOIN materials AS m ON m.material_id = im.material_id
            LEFT JOIN material_analysis AS ma
              ON ma.issue_id = im.issue_id AND ma.material_id = im.material_id
            JOIN material_quality AS mq
              ON mq.issue_id = im.issue_id AND mq.material_id = im.material_id
            WHERE im.issue_id = ?
            ORDER BY im.sort_order, im.material_id
            """,
            (issue_id,),
        )
    )
    if [row["sort_order"] for row in material_rows] != list(range(len(material_rows))):
        raise PublicIssueValidationError("material sort order must be contiguous from zero")
    if len(material_rows) != cast(int, stats["included"]):
        raise PublicIssueValidationError("database material count differs from stats.included")
    if bool(material_rows) == (issue["empty_reason"] is not None):
        raise PublicIssueValidationError("empty_reason must exist exactly for an empty issue")

    rubric_rows = _rows(
        connection.execute(
            """
            SELECT mr.material_id, mr.rubric_id
            FROM material_rubrics AS mr
            JOIN rubrics AS r ON r.rubric_id = mr.rubric_id
            WHERE mr.issue_id = ?
            ORDER BY mr.material_id, r.sort_order, mr.rubric_id
            """,
            (issue_id,),
        )
    )
    rubrics: dict[str, list[JsonValue]] = {}
    for row in rubric_rows:
        rubrics.setdefault(cast(str, row["material_id"]), []).append(cast(str, row["rubric_id"]))

    issue_day = date.fromisoformat(requested_date)
    materials: list[dict[str, object]] = []
    for row in material_rows:
        status = cast(str, row["publication_date_status"])
        published_at = _database_timestamp(
            row["published_at"],
            "database material published_at",
            allow_legacy_date=legacy_inferred,
        )
        if status == "resolved" and published_at is None:
            raise PublicIssueValidationError("resolved material has no published_at")
        if status == "unresolved" and published_at is not None:
            raise PublicIssueValidationError("unresolved material unexpectedly has published_at")
        if row["quality_date_status"] != status:
            raise PublicIssueValidationError("material and quality date statuses differ")
        expected_delta: int | None = None
        if published_at is not None:
            published_day = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").date()
            known_legacy_anomaly = (
                legacy_inferred
                and row["review_status"] == "queued"
                and row["severity"] in {"medium", "high"}
            )
            if published_day > issue_day + timedelta(days=1) and not known_legacy_anomaly:
                raise PublicIssueValidationError("material publication date is after issue window")
            expected_delta = (published_day - issue_day).days
        if row["issue_date_delta_days"] != expected_delta:
            raise PublicIssueValidationError("material quality date delta is inconsistent")
        material_id = cast(str, row["material_id"])
        material_llm_status = row["llm_status"]
        material_effective_model = row["effective_model"]
        if material_llm_status is None:
            material_llm_status = (
                "unavailable" if issue_llm["status"] == "unavailable" else "fallback"
            )
            material_effective_model = None
        materials.append(
            {
                "agpmTakeaway": row["agpm_takeaway"],
                "brief": row["brief"],
                "canonicalUrl": row["canonical_url"],
                "id": material_id,
                "issueDate": requested_date,
                "keyMaterial": bool(row["key_material"]),
                "llm": {
                    "effectiveModel": material_effective_model,
                    "status": material_llm_status,
                },
                "perimeter": row["perimeter"],
                "publicationDateStatus": status,
                "publishedAt": published_at,
                "rubrics": rubrics.get(material_id, []),
                "signalScore": row["signal_score"],
                "signalStrength": row["signal_strength"],
                "sourceName": row["source_name"],
                "summary": row["summary"],
                "theses": _json(row["theses_json"], "material theses"),
                "title": row["title"],
                "trendNotes": row["trend_notes"],
                "url": row["url"],
                "verdict": row["verdict"],
            }
        )

    raw_analysis = _json(analysis_row["analysis_json"], "issue analysis")
    raw_theses = _json(analysis_row["theses_json"], "issue theses")
    document: dict[str, object] = {
        "analysis": {
            "blocks": _analysis_blocks(
                raw_analysis,
                len(materials),
                stats,
                cast(str, analysis_row["llm_status"]),
            ),
            "brief": analysis_row["brief"] if analysis_row["brief"] is not None else issue["brief"],
            "headline": analysis_row["headline"],
        },
        "brief": issue["brief"],
        "issueDate": requested_date,
        "issueNumber": issue["issue_number"],
        "llm": issue_llm,
        "materialCount": len(materials),
        "materials": materials,
        "publishedAt": issue_published_at,
        "stats": stats,
        "theses": raw_theses,
        "title": issue["title"],
    }
    return validate_public_issue_document(document)


def build_public_issue_from_views(
    connection: sqlite3.Connection,
    *,
    issue_date: str,
) -> JsonObject:
    """Build one explicit IssueDetail using only versioned published public views."""
    requested_date = _date(issue_date, "requested issue date")
    issue = _row(
        connection.execute(
            """
            SELECT issue_id, issue_date, issue_number, title, brief, published_at,
                   publication_origin, empty_reason
            FROM pub_issues_v1
            WHERE issue_date = ?
            """,
            (requested_date,),
        ),
        "published issue",
    )
    legacy_inferred = issue["publication_origin"] == "legacy_inferred"
    issue_published_at = _database_timestamp(
        issue["published_at"],
        "public issue published_at",
        allow_legacy_date=legacy_inferred,
    )
    if issue["publication_origin"] == "v2" and issue_published_at is None:
        raise PublicIssueValidationError("V2 published issue has no published_at")
    issue_id = cast(str, issue["issue_id"])
    stats = _stats(
        _row(
            connection.execute(
                """
                SELECT viewed, included, cut, near, mid, far, core, adjacent
                FROM pub_stats_v1
                WHERE issue_id = ?
                """,
                (issue_id,),
            ),
            "published stats",
        ),
        "public stats",
    )
    analysis_row = _row(
        connection.execute(
            """
            SELECT headline, analysis_json, theses_json, brief, llm_status, effective_model
            FROM pub_issue_analysis_v1
            WHERE issue_id = ?
            """,
            (issue_id,),
        ),
        "published issue analysis",
    )
    issue_llm = {
        "effectiveModel": analysis_row["effective_model"],
        "status": analysis_row["llm_status"],
    }
    _llm(issue_llm, "public issue llm")

    material_rows = _rows(
        connection.execute(
            """
            SELECT im.sort_order, im.perimeter, im.verdict, im.summary,
                   im.agpm_takeaway, im.brief, im.theses_json, im.trend_notes,
                   im.key_material, im.signal_score, im.signal_strength,
                   im.material_id, im.title, im.url, im.canonical_url, im.source_name,
                   im.material_published_at, im.publication_date_status,
                   ma.llm_status, ma.effective_model,
                   mq.publication_date_status AS quality_date_status,
                   mq.issue_date_delta_days, mq.severity, mq.review_status
            FROM pub_issue_materials_v1 AS im
            LEFT JOIN pub_material_analysis_v1 AS ma
              ON ma.issue_id = im.issue_id AND ma.material_id = im.material_id
            JOIN pub_material_quality_v1 AS mq
              ON mq.issue_id = im.issue_id AND mq.material_id = im.material_id
            WHERE im.issue_id = ?
            ORDER BY im.sort_order, im.material_id
            """,
            (issue_id,),
        )
    )
    if [row["sort_order"] for row in material_rows] != list(range(len(material_rows))):
        raise PublicIssueValidationError("public material sort order must be contiguous from zero")
    if len(material_rows) != cast(int, stats["included"]):
        raise PublicIssueValidationError("public material count differs from stats.included")
    if bool(material_rows) == (issue["empty_reason"] is not None):
        raise PublicIssueValidationError("empty_reason must exist exactly for an empty issue")

    rubric_rows = _rows(
        connection.execute(
            """
            SELECT material_id, rubric_id
            FROM pub_material_rubrics_v1
            WHERE issue_id = ?
            ORDER BY material_id, sort_order, rubric_id
            """,
            (issue_id,),
        )
    )
    rubrics: dict[str, list[JsonValue]] = {}
    for row in rubric_rows:
        rubrics.setdefault(cast(str, row["material_id"]), []).append(cast(str, row["rubric_id"]))

    issue_day = date.fromisoformat(requested_date)
    materials: list[dict[str, object]] = []
    for row in material_rows:
        status = cast(str, row["publication_date_status"])
        published_at = _database_timestamp(
            row["material_published_at"],
            "public material published_at",
            allow_legacy_date=legacy_inferred,
        )
        if status == "resolved" and published_at is None:
            raise PublicIssueValidationError("resolved public material has no published_at")
        if status == "unresolved" and published_at is not None:
            raise PublicIssueValidationError(
                "unresolved public material unexpectedly has published_at"
            )
        if row["quality_date_status"] != status:
            raise PublicIssueValidationError("public material and quality date statuses differ")
        expected_delta: int | None = None
        if published_at is not None:
            published_day = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").date()
            known_legacy_anomaly = (
                legacy_inferred
                and row["review_status"] == "queued"
                and row["severity"] in {"medium", "high"}
            )
            if published_day > issue_day + timedelta(days=1) and not known_legacy_anomaly:
                raise PublicIssueValidationError(
                    "public material publication date is after issue window"
                )
            expected_delta = (published_day - issue_day).days
        if row["issue_date_delta_days"] != expected_delta:
            raise PublicIssueValidationError("public material quality date delta is inconsistent")
        material_llm_status = row["llm_status"]
        material_effective_model = row["effective_model"]
        if material_llm_status is None:
            material_llm_status = (
                "unavailable" if issue_llm["status"] == "unavailable" else "fallback"
            )
            material_effective_model = None
        material_id = cast(str, row["material_id"])
        materials.append(
            {
                "agpmTakeaway": row["agpm_takeaway"],
                "brief": row["brief"],
                "canonicalUrl": row["canonical_url"],
                "id": material_id,
                "issueDate": requested_date,
                "keyMaterial": bool(row["key_material"]),
                "llm": {
                    "effectiveModel": material_effective_model,
                    "status": material_llm_status,
                },
                "perimeter": row["perimeter"],
                "publicationDateStatus": status,
                "publishedAt": published_at,
                "rubrics": rubrics.get(material_id, []),
                "signalScore": row["signal_score"],
                "signalStrength": row["signal_strength"],
                "sourceName": row["source_name"],
                "summary": row["summary"],
                "theses": _json(row["theses_json"], "public material theses"),
                "title": row["title"],
                "trendNotes": row["trend_notes"],
                "url": row["url"],
                "verdict": row["verdict"],
            }
        )

    raw_analysis = _json(analysis_row["analysis_json"], "public issue analysis")
    document: dict[str, object] = {
        "analysis": {
            "blocks": _analysis_blocks(
                raw_analysis,
                len(materials),
                stats,
                cast(str, analysis_row["llm_status"]),
            ),
            "brief": analysis_row["brief"] if analysis_row["brief"] is not None else issue["brief"],
            "headline": analysis_row["headline"],
        },
        "brief": issue["brief"],
        "issueDate": requested_date,
        "issueNumber": issue["issue_number"],
        "llm": issue_llm,
        "materialCount": len(materials),
        "materials": materials,
        "publishedAt": issue_published_at,
        "stats": stats,
        "theses": _json(analysis_row["theses_json"], "public issue theses"),
        "title": issue["title"],
    }
    return validate_public_issue_document(document)


__all__ = [
    "PublicIssueValidationError",
    "build_public_issue",
    "build_public_issue_from_views",
    "validate_public_issue_document",
    "validate_public_value",
    "verify_public_database_connection",
]
