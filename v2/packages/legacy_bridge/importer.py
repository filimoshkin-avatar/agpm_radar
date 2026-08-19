"""Bootstrap-only, read-only Legacy SQLite to Radar V2 importer."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast
from urllib.parse import quote, urlparse

from packages.contracts import CONTRACT_VERSION
from packages.storage.hashing import (
    DatabaseDigest,
    database_digest,
    file_sha256,
    logical_state_hash,
    rebuild_and_check_fts,
    verify_database,
)
from packages.storage.migrations import EMPTY_SHA256, configure_staging_connection

type JsonObject = dict[str, object]

REQUIRED_EVIDENCE_KINDS: Final = frozenset(
    {"canonical_report", "raw_docx", "normalized_json", "public_json"}
)
LOCAL_ROOTS: Final = ("root", "mnt", "etc", "srv", "opt", "var")
LOCAL_PATH_PATTERN: Final = re.compile(
    r"(?<![\w:])/(?:" + "|".join(LOCAL_ROOTS) + r")(?:/[^\s\"'<>]*)?"
)
SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
BOOTSTRAP_COMPATIBILITY_ID: Final = "app_radar_v2_stage3"
TABLE_COVERAGE: Final[dict[str, tuple[str, str, str | None]]] = {
    "schema_migrations": ("V2 migration runner", "checksum-pinned migration metadata", None),
    "application_compatibility": ("V2 Stage 3 bootstrap", "explicit compatibility marker", None),
    "content_releases": ("bootstrap seal", "sequence-zero immutable release marker", None),
    "source_snapshots": (
        "Legacy DB + frozen evidence manifest",
        "safe aggregate snapshot metadata",
        None,
    ),
    "sources": (
        "Legacy sources + materials + deferred/metadata-only rows",
        "source rows plus deterministic material-source derivation",
        None,
    ),
    "materials": (
        "Legacy materials + deferred queue + source_metadata",
        "safe fields; unassigned states stay non-public; local paths omitted",
        None,
    ),
    "material_sources": ("Legacy materials/sources", "normalized source membership", None),
    "material_evidence": (
        "Legacy source_metadata",
        "safe metadata projection; snapshot path omitted",
        "allowed when source_metadata is empty",
    ),
    "editorial_queue": (
        "deferred JSONL + material_date_quality",
        "deferred/review queue projection",
        "allowed when files and queued quality rows are empty; Legacy has no separate manual queue",
    ),
    "issues": ("Legacy issues + frozen publication manifest", "explicit lifecycle inference", None),
    "legacy_issue_provenance": (
        "Legacy issues + frozen manifest",
        "immutable lifecycle provenance",
        None,
    ),
    "legacy_publication_evidence": (
        "frozen publication manifest",
        "four artifact plus row/integrity/range evidence records",
        None,
    ),
    "issue_materials": (
        "Legacy materials.radar_issue_date",
        "normalized issue membership",
        "allowed for explicit empty issues only",
    ),
    "issue_analysis": (
        "Legacy daily/period analysis + issue_llm_theses",
        "combined daily, 7d/30d and theses outcome",
        None,
    ),
    "material_analysis": (
        "Legacy material_llm_summaries",
        "outcome summary projection",
        "allowed when Legacy summaries are empty",
    ),
    "llm_attempts": (
        "Legacy LLM result tables",
        "one immutable attempt per stored outcome",
        "allowed when all Legacy LLM tables are empty",
    ),
    "source_rules": (
        "Legacy source_domain_rules",
        "direct safe projection",
        "allowed when Legacy rules are empty",
    ),
    "material_quality": (
        "Legacy material_date_quality",
        "issue-normalized quality projection",
        "allowed when Legacy quality is empty",
    ),
    "rubrics": ("Legacy rubrics", "application-owned vocabulary import", None),
    "material_rubrics": (
        "Legacy material_rubrics",
        "issue-normalized rubric membership",
        "allowed when Legacy rubric links are empty",
    ),
    "daily_stats": ("Legacy daily_stats", "issue-key normalized projection", None),
    "gazettes": (
        "explicit Legacy gazette asset",
        "metadata derived from frozen asset arguments",
        "allowed only when no gazette asset is supplied",
    ),
    "gazette_assets": (
        "explicit Legacy gazette asset",
        "content-addressed safe relative asset",
        "allowed only when no gazette asset is supplied",
    ),
}


class LegacyImportError(RuntimeError):
    """The Legacy corpus cannot be imported without violating the contract."""


class BootstrapSealedError(LegacyImportError):
    """The target has crossed release zero and the importer is disabled forever."""


@dataclass(frozen=True, slots=True)
class GazetteInput:
    """Explicit evidence needed to represent the one Legacy static gazette."""

    path: Path
    relative_path: str
    period: str
    title: str
    published_at: str


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    """Import source, derivation and resulting canonical evidence for one table."""

    source: str
    derivation: str
    allowed_empty_evidence: str | None
    row_count: int
    table_hash: str


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Complete deterministic Stage 3 import gate evidence."""

    source_sha256: str
    evidence_manifest_sha256: str
    inferred_published_issues: int
    ambiguous_draft_issues: int
    state_hash: str
    coverage: dict[str, CoverageRecord]
    digest: DatabaseDigest


def deterministic_id(namespace: str, legacy_key: str) -> str:
    """Produce an explicit stable identifier independent of rowid or import order."""
    prefix = {
        "issue": "iss",
        "material": "mat",
        "source": "src",
        "evidence": "evd",
        "queue": "que",
        "attempt": "llm",
        "snapshot": "snp",
        "release": "rel",
        "candidate": "can",
        "gazette": "gaz",
    }.get(namespace, "id")
    digest = hashlib.sha256(f"radar-v2:{namespace}:{legacy_key}".encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def canonical_json(value: object) -> str:
    """Serialize JSON columns with stable key ordering and no platform whitespace."""
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def strip_local_paths(value: str | None) -> str | None:
    """Remove host-local absolute paths from otherwise safe textual metadata."""
    if value is None:
        return None
    return LOCAL_PATH_PATTERN.sub("[local-path-removed]", value)


def normalize_timestamp(value: object, *, fallback: str) -> str:
    """Normalize Legacy UTC-ish timestamps while keeping time generation external."""
    if value is None or str(value).strip() == "":
        return fallback
    text = str(value).strip()
    if len(text) == 19 and text[10] == " ":
        return text[:10] + "T" + text[11:] + "Z"
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def safe_url(value: object, *, required: bool) -> str | None:
    if value is None or str(value).strip() == "":
        if required:
            raise LegacyImportError("required material URL is empty")
        return None
    text = str(value).strip()
    if urlparse(text).scheme.lower() not in {"http", "https"}:
        if required:
            raise LegacyImportError(f"unsafe required URL scheme: {text!r}")
        return None
    return text


def safe_relative_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise LegacyImportError(f"unsafe relative path: {value!r}")
    return path.as_posix()


def _read_json_object(path: Path) -> tuple[JsonObject, str]:
    raw = path.read_bytes()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise LegacyImportError(f"JSON root must be an object: {path}")
    return cast(JsonObject, parsed), hashlib.sha256(raw).hexdigest()


def _manifest_issues(manifest: JsonObject) -> dict[str, JsonObject]:
    issues = manifest.get("issues")
    if not isinstance(issues, list):
        raise LegacyImportError("publication evidence manifest has no issues array")
    result: dict[str, JsonObject] = {}
    for raw_issue in issues:
        if not isinstance(raw_issue, dict):
            raise LegacyImportError("publication evidence issue is not an object")
        issue = cast(JsonObject, raw_issue)
        issue_date = str(issue.get("issueDate", ""))
        if not issue_date or issue_date in result:
            raise LegacyImportError(f"invalid/duplicate evidence issue date: {issue_date!r}")
        result[issue_date] = issue
    if int(str(manifest.get("issueCount", -1))) != len(result):
        raise LegacyImportError("publication evidence issue count mismatch")
    return result


def _open_legacy_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()))}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    if str(connection.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
        connection.close()
        raise LegacyImportError("Legacy database quick_check failed")
    if tuple(connection.execute("PRAGMA foreign_key_check")):
        connection.close()
        raise LegacyImportError("Legacy database foreign_key_check failed")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _rows(
    connection: sqlite3.Connection, query: str, parameters: tuple[object, ...] = ()
) -> tuple[sqlite3.Row, ...]:
    return tuple(connection.execute(query, parameters))


def _json_or_default(value: object, default: object) -> object:
    try:
        parsed = json.loads(str(value)) if value is not None else default
    except json.JSONDecodeError:
        return default
    return parsed


def _is_deterministic_outcome(row: sqlite3.Row) -> bool:
    """Identify stored rule-based output that must not masquerade as an LLM call."""
    keys = set(row.keys())
    provider = str(row["provider"] or "").casefold() if "provider" in keys else ""
    model = str(row["model"] or "").casefold() if "model" in keys else ""
    raw_status = str(row["status"] or "").casefold() if "status" in keys else ""
    return (
        provider == "fallback"
        or model.startswith(("rules-", "deterministic-"))
        or raw_status == "deterministic"
    )


def _assert_unsealed(target: sqlite3.Connection) -> None:
    release_count = int(target.execute("SELECT COUNT(*) FROM content_releases").fetchone()[0])
    if release_count:
        raise BootstrapSealedError("bootstrap importer permanently disabled after release zero")
    for table in ("issues", "materials", "source_snapshots", "gazettes"):
        query = f'SELECT COUNT(*) FROM "{table}"'  # noqa: S608 -- fixed local allowlist
        if int(target.execute(query).fetchone()[0]):
            raise LegacyImportError(f"bootstrap target is not empty: {table}")


def _issue_evidence_passes(issue: JsonObject, material_count: int) -> bool:
    evidence = issue.get("evidence")
    if not isinstance(evidence, list):
        return False
    kinds = {
        str(item.get("kind"))
        for item in evidence
        if isinstance(item, dict)
        and SHA256_PATTERN.fullmatch(str(item.get("sha256", ""))) is not None
    }
    return (
        kinds >= REQUIRED_EVIDENCE_KINDS
        and bool(issue.get("statsInvariantPassed"))
        and int(str(issue.get("materialCount", -1))) == material_count
        and SHA256_PATTERN.fullmatch(str(issue.get("legacyIssueRowSha256", ""))) is not None
    )


def _insert_issues(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    evidence_by_date: dict[str, JsonObject],
    baseline_sha256: str,
    imported_at: str,
) -> tuple[dict[str, str], int, int]:
    legacy_issues = _rows(source, "SELECT * FROM issues ORDER BY issue_date")
    material_counts = {
        str(row[0]): int(row[1])
        for row in source.execute(
            "SELECT radar_issue_date, COUNT(*) FROM materials GROUP BY radar_issue_date"
        )
    }
    issue_ids: dict[str, str] = {}
    published = 0
    ambiguous = 0
    for row in legacy_issues:
        issue_date = str(row["issue_date"])
        issue_id = deterministic_id("issue", issue_date)
        issue_ids[issue_date] = issue_id
        evidence = evidence_by_date.get(issue_date)
        count = material_counts.get(issue_date, 0)
        inferred = evidence is not None and _issue_evidence_passes(evidence, count)
        lifecycle_status = "published" if inferred else "draft"
        publication_origin = "legacy_inferred" if inferred else None
        published += int(inferred)
        ambiguous += int(not inferred)
        issue_payload = {
            "brief": strip_local_paths(cast(str | None, row["brief"])),
            "date": issue_date,
            "material_ids": [
                deterministic_id("material", str(material[0]))
                for material in source.execute(
                    "SELECT id FROM materials WHERE radar_issue_date = ? ORDER BY id", (issue_date,)
                )
            ],
            "title": strip_local_paths(str(row["title"] or f"Radar {issue_date}")),
        }
        created_at = normalize_timestamp(row["created_at"], fallback=imported_at)
        updated_at = normalize_timestamp(row["updated_at"], fallback=created_at)
        target.execute(
            """
            INSERT INTO issues(
              issue_id, issue_date, issue_number, title, brief, lifecycle_status,
              published_at, publication_origin, empty_reason, content_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue_id,
                issue_date,
                row["issue_number"],
                issue_payload["title"],
                issue_payload["brief"],
                lifecycle_status,
                None,
                publication_origin,
                "legacy-explicit-empty" if count == 0 else None,
                content_hash(issue_payload),
                created_at,
                updated_at,
            ),
        )
        legacy_issue_columns = row.keys()
        frozen_row_hash = (
            str(evidence["legacyIssueRowSha256"])
            if evidence is not None
            else content_hash(
                {key: row[key] for key in legacy_issue_columns if not key.endswith("_path")}
            )
        )
        target.execute(
            """
            INSERT INTO legacy_issue_provenance(
              issue_id, legacy_status, legacy_published_at, baseline_database_sha256,
              legacy_issue_row_sha256, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                issue_id,
                row["status"],
                normalize_timestamp(row["published_at"], fallback=imported_at)
                if row["published_at"]
                else None,
                baseline_sha256,
                frozen_row_hash,
                imported_at,
            ),
        )
        _insert_publication_evidence(target, issue_id, issue_date, evidence, count, inferred)
    return issue_ids, published, ambiguous


def _insert_publication_evidence(
    target: sqlite3.Connection,
    issue_id: str,
    issue_date: str,
    evidence: JsonObject | None,
    material_count: int,
    passed: bool,
) -> None:
    artifact_records: dict[str, JsonObject] = {}
    if evidence is not None and isinstance(evidence.get("evidence"), list):
        for raw in cast(list[object], evidence["evidence"]):
            if isinstance(raw, dict):
                artifact_record = cast(JsonObject, raw)
                artifact_records[str(artifact_record.get("kind"))] = artifact_record
    for kind in sorted(REQUIRED_EVIDENCE_KINDS):
        selected_artifact = artifact_records.get(kind)
        status = "passed" if selected_artifact is not None else "failed"
        relative_path = (
            safe_relative_path(str(selected_artifact["relativePath"]))
            if selected_artifact
            else f"missing/{issue_date}/{kind}"
        )
        digest = str(selected_artifact["sha256"]) if selected_artifact else EMPTY_SHA256
        stored_kind = "generated_public_json" if kind == "public_json" else kind
        target.execute(
            "INSERT INTO legacy_publication_evidence VALUES (?, ?, ?, ?, ?, ?)",
            (
                issue_id,
                stored_kind,
                relative_path,
                digest,
                status,
                canonical_json({"manifest_kind": kind}),
            ),
        )
    synthetic: tuple[tuple[str, str, str, bool, JsonObject], ...] = (
        (
            "baseline_issue_row",
            f"legacy-db/issues/{issue_date}",
            str(evidence.get("legacyIssueRowSha256")) if evidence else EMPTY_SHA256,
            evidence is not None,
            {},
        ),
        (
            "integrity",
            f"frozen-manifest/integrity/{issue_date}",
            content_hash(
                {
                    "material_count": material_count,
                    "stats": bool(evidence and evidence.get("statsInvariantPassed")),
                }
            ),
            bool(evidence and evidence.get("statsInvariantPassed")),
            {"material_count": material_count},
        ),
        (
            "baseline_range",
            f"frozen-manifest/range/{issue_date}",
            content_hash({"date": issue_date}),
            evidence is not None,
            {"allowlisted": evidence is not None, "publication_inferred": passed},
        ),
    )
    for kind, relative_path, digest, evidence_passed, details in synthetic:
        target.execute(
            "INSERT INTO legacy_publication_evidence VALUES (?, ?, ?, ?, ?, ?)",
            (
                issue_id,
                kind,
                relative_path,
                digest,
                "passed" if evidence_passed else "failed",
                canonical_json(details),
            ),
        )


def _source_key(row: sqlite3.Row) -> str:
    return str(
        row["source_id"] or row["source_name"] or urlparse(str(row["url"])).hostname or "unknown"
    )


def _insert_materials_and_sources(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    issue_ids: dict[str, str],
    imported_at: str,
) -> dict[str, str]:
    source_rows = (
        _rows(source, "SELECT * FROM sources ORDER BY id")
        if _table_exists(source, "sources")
        else ()
    )
    source_details = {str(row["id"]): row for row in source_rows}
    material_rows = _rows(
        source,
        "SELECT rowid AS legacy_rowid, * FROM materials ORDER BY radar_issue_date, rowid, id",
    )
    source_keys = sorted({_source_key(row) for row in material_rows} | set(source_details))
    source_ids = {key: deterministic_id("source", key) for key in source_keys}
    for key in source_keys:
        detail = source_details.get(key)
        matching = next((row for row in material_rows if _source_key(row) == key), None)
        name = str(
            detail["name"] if detail is not None else matching["source_name"] if matching else key
        )
        url = safe_url(detail["url"], required=False) if detail is not None else None
        updated = max(
            (
                normalize_timestamp(row["updated_at"], fallback=imported_at)
                for row in material_rows
                if _source_key(row) == key
            ),
            default=imported_at,
        )
        target.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?)",
            (source_ids[key], strip_local_paths(name) or key, url, "legacy", 1, updated),
        )

    material_ids: dict[str, str] = {}
    order_by_issue: dict[str, int] = {}
    for row in material_rows:
        legacy_id = str(row["id"])
        material_id = deterministic_id("material", legacy_id)
        material_ids[legacy_id] = material_id
        material_payload = {
            "agpm_takeaway": strip_local_paths(cast(str | None, row["agpm_takeaway"])),
            "brief": strip_local_paths(cast(str | None, row["brief"])),
            "canonical_url": safe_url(row["canonical_url"], required=False),
            "publication_date_status": str(row["publication_date_status"] or "unresolved"),
            "published_at": normalize_timestamp(row["published_at"], fallback=imported_at)
            if row["published_at"]
            else None,
            "source_name": strip_local_paths(cast(str | None, row["source_name"])),
            "summary": strip_local_paths(cast(str | None, row["summary"])),
            "title": strip_local_paths(str(row["title"])),
            "url": safe_url(row["url"], required=True),
        }
        created = normalize_timestamp(row["created_at"], fallback=imported_at)
        updated = normalize_timestamp(row["updated_at"], fallback=created)
        target.execute(
            """
            INSERT INTO materials(
              material_id, title, url, canonical_url, source_name, published_at,
              publication_date_status, summary, agpm_takeaway, brief, content_hash,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                material_id,
                material_payload["title"],
                material_payload["url"],
                material_payload["canonical_url"],
                material_payload["source_name"],
                material_payload["published_at"],
                material_payload["publication_date_status"],
                material_payload["summary"],
                material_payload["agpm_takeaway"],
                material_payload["brief"],
                content_hash(material_payload),
                created,
                updated,
            ),
        )
        key = _source_key(row)
        target.execute(
            "INSERT INTO material_sources VALUES (?, ?, ?, ?, ?, ?)",
            (
                material_id,
                source_ids[key],
                material_payload["url"],
                "legacy",
                normalize_timestamp(row["first_seen_at"], fallback=created)
                if row["first_seen_at"]
                else None,
                updated,
            ),
        )
        issue_date = str(row["radar_issue_date"])
        issue_id = issue_ids.get(issue_date)
        if issue_id is None:
            raise LegacyImportError(f"material references missing Legacy issue: {issue_date}")
        sort_order = order_by_issue.get(issue_date, 0)
        order_by_issue[issue_date] = sort_order + 1
        flags = {
            "governance": bool(row["governance_flag"]),
            "human_in_the_loop": bool(row["human_in_the_loop_flag"]),
            "isup": bool(row["isup_flag"]),
            "mcp": bool(row["mcp_flag"]),
            "pmo": bool(row["pmo_flag"]),
            "security": bool(row["security_flag"]),
        }
        target.execute(
            """
            INSERT INTO issue_materials(
              issue_id, material_id, sort_order, perimeter, verdict, summary, agpm_takeaway,
              brief, theses_json, trend_notes, flags_json, key_material, signal_score,
              signal_strength, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue_id,
                material_id,
                sort_order,
                row["perimeter"] if row["perimeter"] in {"near", "mid", "far"} else "far",
                row["verdict"] if row["verdict"] in {"core", "adjacent"} else "adjacent",
                material_payload["summary"],
                material_payload["agpm_takeaway"],
                material_payload["brief"],
                canonical_json(_json_or_default(row["theses_json"], [])),
                strip_local_paths(cast(str | None, row["trend_notes"])),
                canonical_json(flags),
                int(bool(row["key_material"])),
                row["signal_score"],
                row["signal_strength"]
                if row["signal_strength"] in {"strong", "context", "watch"}
                else "strong",
                created,
                updated,
            ),
        )
    return material_ids


def _insert_source_metadata(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    material_ids: dict[str, str],
    material_ids_by_url: dict[str, str],
    imported_at: str,
) -> None:
    if not _table_exists(source, "source_metadata"):
        return
    by_url: dict[str, str] = {}
    for row in source.execute("SELECT id, url, canonical_url FROM materials"):
        material_id = material_ids[str(row["id"])]
        by_url[str(row["url"])] = material_id
        material_ids_by_url[str(row["url"])] = material_id
        if row["canonical_url"]:
            by_url[str(row["canonical_url"])] = material_id
            material_ids_by_url[str(row["canonical_url"])] = material_id
    for row in source.execute("SELECT * FROM source_metadata ORDER BY url"):
        matched_material_id = (
            by_url.get(str(row["url"]))
            or by_url.get(str(row["canonical_url"]))
            or material_ids_by_url.get(str(row["url"]))
            or material_ids_by_url.get(str(row["canonical_url"]))
        )
        if matched_material_id is None:
            metadata_url = safe_url(row["url"], required=True)
            if metadata_url is None:
                raise LegacyImportError("source metadata URL unexpectedly normalized to null")
            metadata_host = urlparse(metadata_url).hostname or "metadata-only"
            matched_material_id = _insert_unassigned_material(
                target,
                legacy_key=f"source-metadata:{metadata_url}",
                url=metadata_url,
                canonical_url=safe_url(row["canonical_url"], required=False),
                title=str(row["title"] or metadata_url),
                source_key=metadata_host,
                source_name=metadata_host,
                source_url=None,
                source_type="legacy-metadata-only",
                published_at=(
                    normalize_timestamp(row["extracted_published_at"], fallback=imported_at)
                    if row["extracted_published_at"]
                    else None
                ),
                publication_date_status=str(row["status"] or "unresolved"),
                summary=None,
                created_at=normalize_timestamp(row["fetched_at"], fallback=imported_at),
                updated_at=normalize_timestamp(row["fetched_at"], fallback=imported_at),
            )
            by_url[metadata_url] = matched_material_id
            material_ids_by_url[metadata_url] = matched_material_id
        metadata = {
            "canonical_url": safe_url(row["canonical_url"], required=False),
            "confidence": row["confidence"],
            "content_type": strip_local_paths(cast(str | None, row["content_type"])),
            "error": strip_local_paths(cast(str | None, row["error"])),
            "extracted_published_at": row["extracted_published_at"],
            "extraction_source": row["extraction_source"],
            "fetched_at": row["fetched_at"],
            "http_status": row["http_status"],
            "status": row["status"],
        }
        digest = content_hash(metadata)
        target.execute(
            "INSERT INTO material_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                deterministic_id("evidence", str(row["url"])),
                matched_material_id,
                "legacy-source-metadata",
                digest,
                "application/json",
                safe_url(row["url"], required=False),
                canonical_json(metadata),
                normalize_timestamp(row["fetched_at"], fallback=imported_at),
            ),
        )


def _insert_analysis_and_attempts(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    issue_ids: dict[str, str],
    material_ids: dict[str, str],
    imported_at: str,
) -> None:
    llm_theses = (
        {str(row["issue_date"]): row for row in _rows(source, "SELECT * FROM issue_llm_theses")}
        if _table_exists(source, "issue_llm_theses")
        else {}
    )
    period_rows = (
        _rows(
            source,
            "SELECT * FROM issue_period_theses ORDER BY as_of_issue_date, period",
        )
        if _table_exists(source, "issue_period_theses")
        else ()
    )
    periods_by_issue: dict[str, list[sqlite3.Row]] = {}
    for period_row in period_rows:
        periods_by_issue.setdefault(str(period_row["as_of_issue_date"]), []).append(period_row)
    issue_rows = (
        _rows(source, "SELECT * FROM issue_daily_analysis ORDER BY issue_date")
        if _table_exists(source, "issue_daily_analysis")
        else ()
    )
    issue_fallback = {
        str(row["issue_date"]): row
        for row in source.execute("SELECT issue_date, theses_json, brief, updated_at FROM issues")
    }
    for row in issue_rows:
        issue_date = str(row["issue_date"])
        theses = llm_theses.get(issue_date)
        fallback = issue_fallback[issue_date]
        status = str(row["status"])
        llm_status = (
            "success"
            if status == "success"
            else "fallback"
            if status in {"fallback", "deterministic"}
            else "unavailable"
        )
        deterministic = _is_deterministic_outcome(row)
        requested_model = None if deterministic or status == "fallback" else row["model"]
        effective_model = row["model"] if llm_status != "unavailable" else None
        period_analysis = {
            str(period["period"]): {
                "brief": strip_local_paths(cast(str | None, period["brief"])),
                "end_issue_date": period["end_issue_date"],
                "issue_count": period["issue_count"],
                "material_count": period["material_count"],
                "model": period["model"],
                "prompt_version": period["prompt_version"],
                "provider": period["provider"],
                "start_issue_date": period["start_issue_date"],
                "stats": _json_or_default(period["stats_json"], {}),
                "theses": _json_or_default(period["theses_json"], []),
            }
            for period in periods_by_issue.get(issue_date, [])
        }
        target.execute(
            "INSERT INTO issue_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                issue_ids[issue_date],
                strip_local_paths(cast(str | None, row["headline"])),
                canonical_json(
                    {
                        "daily": _json_or_default(row["analysis_json"], {}),
                        "legacy_periods": period_analysis,
                    }
                ),
                canonical_json(
                    _json_or_default(
                        theses["theses_json"] if theses else fallback["theses_json"], []
                    )
                ),
                strip_local_paths(
                    cast(str | None, theses["brief"] if theses else fallback["brief"])
                ),
                llm_status,
                requested_model,
                effective_model,
                row["provider"],
                str(row["prompt_version"]),
                normalize_timestamp(row["updated_at"], fallback=imported_at),
            ),
        )
        _insert_attempt(
            target, "issue-daily", issue_date, issue_ids[issue_date], None, row, 1, imported_at
        )
    for issue_date, row in sorted(llm_theses.items()):
        _insert_attempt(
            target, "issue-theses", issue_date, issue_ids[issue_date], None, row, 2, imported_at
        )
    for row in period_rows:
        issue_date = str(row["as_of_issue_date"])
        _insert_attempt(
            target,
            f"issue-period-{row['period']}",
            issue_date,
            issue_ids[issue_date],
            None,
            row,
            3 if row["period"] == "7d" else 4,
            imported_at,
        )

    summaries = (
        _rows(source, "SELECT * FROM material_llm_summaries ORDER BY material_id")
        if _table_exists(source, "material_llm_summaries")
        else ()
    )
    for row in summaries:
        legacy_material_id = str(row["material_id"])
        issue_date = str(row["issue_date"])
        status = str(row["status"])
        llm_status = (
            "success"
            if status == "success"
            else "fallback"
            if status == "fallback"
            else "unavailable"
        )
        deterministic = _is_deterministic_outcome(row)
        requested_model = None if deterministic or status == "fallback" else row["model"]
        effective_model = row["model"] if llm_status != "unavailable" else None
        target.execute(
            "INSERT INTO material_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                issue_ids[issue_date],
                material_ids[legacy_material_id],
                strip_local_paths(cast(str | None, row["short_text"])),
                strip_local_paths(cast(str | None, row["agpm_angle"])),
                llm_status,
                requested_model,
                effective_model,
                row["provider"],
                str(row["prompt_version"]),
                normalize_timestamp(row["updated_at"], fallback=imported_at),
            ),
        )
        _insert_attempt(
            target,
            "material-summary",
            legacy_material_id,
            issue_ids[issue_date],
            material_ids[legacy_material_id],
            row,
            1,
            imported_at,
        )

    classifications = (
        _rows(source, "SELECT * FROM llm_classifications ORDER BY id")
        if _table_exists(source, "llm_classifications")
        else ()
    )
    issue_by_material = {
        str(row["id"]): issue_ids[str(row["radar_issue_date"])]
        for row in source.execute("SELECT id, radar_issue_date FROM materials")
    }
    for row in classifications:
        legacy_material_id = str(row["material_id"])
        _insert_attempt(
            target,
            "classification",
            str(row["id"]),
            issue_by_material[legacy_material_id],
            material_ids[legacy_material_id],
            row,
            1,
            imported_at,
        )


def _insert_attempt(
    target: sqlite3.Connection,
    kind: str,
    key: str,
    issue_id: str,
    material_id: str | None,
    row: sqlite3.Row,
    order: int,
    imported_at: str,
) -> None:
    keys = set(row.keys())
    raw_status = str(row["status"]) if "status" in keys else "success"
    deterministic = _is_deterministic_outcome(row)
    status = (
        "skipped"
        if deterministic
        else "success"
        if raw_status in {"success", "ok", "fallback"}
        else "error"
        if raw_status in {"error", "failed"}
        else "skipped"
    )
    started = normalize_timestamp(
        row["created_at"] if "created_at" in keys else row["updated_at"], fallback=imported_at
    )
    finished = normalize_timestamp(
        row["updated_at"] if "updated_at" in keys else started, fallback=started
    )
    model = row["model"] if "model" in keys else None
    error = row["error"] if "error" in keys else None
    requested_model = None if deterministic or raw_status == "fallback" else model
    normalized_error = (
        "LEGACY_DETERMINISTIC_FALLBACK"
        if deterministic
        else strip_local_paths(str(error))
        if error
        else None
    )
    target.execute(
        "INSERT INTO llm_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            deterministic_id("attempt", f"{kind}:{key}"),
            "material" if material_id else "issue",
            issue_id,
            material_id,
            requested_model,
            model,
            row["provider"] if "provider" in keys else None,
            order,
            status,
            normalized_error,
            started,
            finished,
        ),
    )


def _insert_quality_rubrics_stats_rules(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    issue_ids: dict[str, str],
    material_ids: dict[str, str],
    imported_at: str,
) -> None:
    issue_by_material = {
        str(row["id"]): issue_ids[str(row["radar_issue_date"])]
        for row in source.execute("SELECT id, radar_issue_date FROM materials")
    }
    if _table_exists(source, "source_domain_rules"):
        for row in source.execute("SELECT * FROM source_domain_rules ORDER BY host"):
            target.execute(
                "INSERT INTO source_rules VALUES (?, ?, ?, ?)",
                (
                    row["host"],
                    row["date_strategy"],
                    strip_local_paths(cast(str | None, row["notes"])),
                    normalize_timestamp(row["updated_at"], fallback=imported_at),
                ),
            )
    if _table_exists(source, "material_date_quality"):
        for row in source.execute("SELECT * FROM material_date_quality ORDER BY material_id"):
            legacy_id = str(row["material_id"])
            issue_id = issue_by_material[legacy_id]
            material_id = material_ids[legacy_id]
            target.execute(
                "INSERT INTO material_quality VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    issue_id,
                    material_id,
                    row["publication_date_status"],
                    row["issue_date_delta_days"],
                    row["severity"]
                    if row["severity"] in {"ok", "low", "medium", "high"}
                    else "low",
                    row["review_status"]
                    if row["review_status"] in {"ok", "monitor", "queued"}
                    else "monitor",
                    strip_local_paths(cast(str | None, row["reason"])),
                    normalize_timestamp(row["updated_at"], fallback=imported_at),
                ),
            )
            if row["review_status"] == "queued":
                target.execute(
                    "INSERT INTO editorial_queue VALUES (?, ?, 'review', ?, 0, ?, ?, ?)",
                    (
                        deterministic_id("queue", f"review:{legacy_id}"),
                        material_id,
                        next(
                            date for date, candidate in issue_ids.items() if candidate == issue_id
                        ),
                        strip_local_paths(cast(str | None, row["reason"])),
                        normalize_timestamp(row["created_at"], fallback=imported_at),
                        normalize_timestamp(row["updated_at"], fallback=imported_at),
                    ),
                )
    if _table_exists(source, "rubrics"):
        for row in source.execute("SELECT * FROM rubrics ORDER BY id"):
            target.execute(
                "INSERT INTO rubrics VALUES (?, ?, ?)", (row["id"], row["title"], row["sort_order"])
            )
    if _table_exists(source, "material_rubrics"):
        for row in source.execute("SELECT * FROM material_rubrics ORDER BY material_id, rubric_id"):
            legacy_id = str(row["material_id"])
            target.execute(
                "INSERT INTO material_rubrics VALUES (?, ?, ?, ?, ?)",
                (
                    issue_by_material[legacy_id],
                    material_ids[legacy_id],
                    row["rubric_id"],
                    row["confidence"],
                    str(row["source"] or "legacy"),
                ),
            )
    for row in source.execute("SELECT * FROM daily_stats ORDER BY stat_date"):
        target.execute(
            "INSERT INTO daily_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                issue_ids[str(row["stat_date"])],
                row["viewed"],
                row["included"],
                row["cut"],
                row["near"],
                row["mid"],
                row["far"],
                row["core"],
                row["adjacent"],
                normalize_timestamp(row["updated_at"], fallback=imported_at),
            ),
        )


def _insert_unassigned_material(
    target: sqlite3.Connection,
    *,
    legacy_key: str,
    url: str,
    canonical_url: str | None,
    title: str,
    source_key: str,
    source_name: str,
    source_url: str | None,
    source_type: str,
    published_at: str | None,
    publication_date_status: str,
    summary: str | None,
    created_at: str,
    updated_at: str,
) -> str:
    """Represent collected-but-unpublished Legacy state without issue membership."""
    material_id = deterministic_id("material", legacy_key)
    source_id = deterministic_id("source", source_key)
    target.execute(
        "INSERT OR IGNORE INTO sources VALUES (?, ?, ?, ?, 1, ?)",
        (
            source_id,
            strip_local_paths(source_name) or source_key,
            safe_url(source_url, required=False),
            source_type,
            updated_at,
        ),
    )
    normalized_status = (
        publication_date_status
        if publication_date_status in {"resolved", "low_confidence", "unresolved"}
        else "unresolved"
    )
    payload = {
        "agpm_takeaway": None,
        "brief": None,
        "canonical_url": canonical_url,
        "publication_date_status": normalized_status,
        "published_at": published_at,
        "source_name": strip_local_paths(source_name),
        "summary": strip_local_paths(summary),
        "title": strip_local_paths(title) or url,
        "url": url,
    }
    target.execute(
        """
        INSERT OR IGNORE INTO materials(
          material_id, title, url, canonical_url, source_name, published_at,
          publication_date_status, summary, agpm_takeaway, brief, content_hash,
          created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            material_id,
            payload["title"],
            payload["url"],
            payload["canonical_url"],
            payload["source_name"],
            payload["published_at"],
            payload["publication_date_status"],
            payload["summary"],
            None,
            None,
            content_hash(payload),
            created_at,
            updated_at,
        ),
    )
    target.execute(
        "INSERT OR IGNORE INTO material_sources VALUES (?, ?, ?, ?, ?, ?)",
        (material_id, source_id, url, source_type, created_at, updated_at),
    )
    return material_id


def _insert_deferred_queue(
    target: sqlite3.Connection,
    *,
    deferred_path: Path | None,
    material_ids_by_url: dict[str, str],
    imported_at: str,
) -> None:
    if deferred_path is None:
        return
    for index, line in enumerate(deferred_path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise LegacyImportError(f"deferred queue row {index + 1} is not an object")
        item = cast(JsonObject, raw)
        url = safe_url(item.get("canonical_url") or item.get("url"), required=True)
        if url is None:
            raise LegacyImportError("deferred queue required URL unexpectedly normalized to null")
        material_id = material_ids_by_url.get(url)
        if material_id is None:
            source_hits = item.get("source_hits")
            first_hit = (
                cast(JsonObject, source_hits[0])
                if isinstance(source_hits, list)
                and source_hits
                and isinstance(source_hits[0], dict)
                else {}
            )
            source_key = str(first_hit.get("source_id") or urlparse(url).hostname or "deferred")
            source_name = str(first_hit.get("source_title") or source_key)
            material_id = _insert_unassigned_material(
                target,
                legacy_key=f"deferred:{item.get('id') or url}",
                url=url,
                canonical_url=safe_url(item.get("canonical_url"), required=False),
                title=str(item.get("title") or url),
                source_key=source_key,
                source_name=source_name,
                source_url=(str(first_hit["source_url"]) if first_hit.get("source_url") else None),
                source_type=str(first_hit.get("provider") or "legacy-deferred"),
                published_at=(
                    normalize_timestamp(item.get("published_at"), fallback=imported_at)
                    if item.get("published_at")
                    else None
                ),
                publication_date_status=("resolved" if item.get("published_at") else "unresolved"),
                summary=cast(str | None, item.get("summary")),
                created_at=normalize_timestamp(item.get("first_seen_at"), fallback=imported_at),
                updated_at=normalize_timestamp(item.get("last_seen_at"), fallback=imported_at),
            )
            material_ids_by_url[url] = material_id
        marker = item.get("_radar_deferred")
        marker_object = cast(JsonObject, marker) if isinstance(marker, dict) else {}
        last_deferred_for = marker_object.get("last_deferred_for")
        reason = "legacy daily deferred queue"
        if last_deferred_for:
            reason += f"; last deferred for {last_deferred_for}"
        target.execute(
            "INSERT INTO editorial_queue VALUES (?, ?, 'deferred', ?, ?, ?, ?, ?)",
            (
                deterministic_id("queue", f"deferred:{url}"),
                material_id,
                None,
                index,
                reason,
                imported_at,
                imported_at,
            ),
        )


def _insert_gazette(
    target: sqlite3.Connection, gazette: GazetteInput | None, imported_at: str
) -> None:
    if gazette is None:
        return
    relative_path = safe_relative_path(gazette.relative_path)
    digest = file_sha256(gazette.path)
    gazette_id = deterministic_id("gazette", gazette.period)
    manifest_hash = content_hash(
        [{"bytes": gazette.path.stat().st_size, "path": relative_path, "sha256": digest}]
    )
    target.execute(
        "INSERT INTO gazettes VALUES (?, ?, ?, 'published', ?, ?, ?, ?, ?)",
        (
            gazette_id,
            gazette.period,
            strip_local_paths(gazette.title),
            gazette.published_at,
            manifest_hash,
            digest,
            imported_at,
            imported_at,
        ),
    )
    target.execute(
        "INSERT INTO gazette_assets VALUES (?, ?, ?, ?, ?)",
        (gazette_id, relative_path, digest, gazette.path.stat().st_size, "text/html"),
    )


def import_legacy(
    *,
    legacy_db: Path,
    target: sqlite3.Connection,
    evidence_manifest: Path,
    expected_manifest_sha256: str,
    imported_at: str,
    deferred_queue: Path | None = None,
    gazette: GazetteInput | None = None,
) -> ImportReport:
    """Perform the one allowed bootstrap import and permanently seal the target."""
    manifest, manifest_sha256 = _read_json_object(evidence_manifest)
    if manifest_sha256 != expected_manifest_sha256:
        raise LegacyImportError("publication evidence manifest SHA-256 mismatch")
    baseline_sha256 = str(manifest.get("baselineDatabaseSha256", ""))
    source_sha256 = file_sha256(legacy_db)
    if not SHA256_PATTERN.fullmatch(baseline_sha256) or source_sha256 != baseline_sha256:
        raise LegacyImportError("Legacy database SHA-256 does not match frozen evidence")
    evidence_by_date = _manifest_issues(manifest)
    source = _open_legacy_read_only(legacy_db)
    configure_staging_connection(target)
    _assert_unsealed(target)
    before_hash = logical_state_hash(target)
    target.execute("BEGIN IMMEDIATE")
    try:
        issue_ids, published, ambiguous = _insert_issues(
            source,
            target,
            evidence_by_date=evidence_by_date,
            baseline_sha256=baseline_sha256,
            imported_at=imported_at,
        )
        material_ids = _insert_materials_and_sources(
            source, target, issue_ids=issue_ids, imported_at=imported_at
        )
        _insert_analysis_and_attempts(
            source, target, issue_ids=issue_ids, material_ids=material_ids, imported_at=imported_at
        )
        _insert_quality_rubrics_stats_rules(
            source, target, issue_ids=issue_ids, material_ids=material_ids, imported_at=imported_at
        )
        material_ids_by_url: dict[str, str] = {}
        for row in source.execute("SELECT id, url, canonical_url FROM materials"):
            material_id = material_ids[str(row["id"])]
            material_ids_by_url[str(row["url"])] = material_id
            if row["canonical_url"]:
                material_ids_by_url[str(row["canonical_url"])] = material_id
        _insert_deferred_queue(
            target,
            deferred_path=deferred_queue,
            material_ids_by_url=material_ids_by_url,
            imported_at=imported_at,
        )
        _insert_source_metadata(
            source,
            target,
            material_ids=material_ids,
            material_ids_by_url=material_ids_by_url,
            imported_at=imported_at,
        )
        _insert_gazette(target, gazette, imported_at)
        target.execute(
            "INSERT INTO source_snapshots VALUES (?, ?, ?, ?, ?)",
            (
                deterministic_id("snapshot", source_sha256),
                manifest_sha256,
                source_sha256,
                imported_at,
                len(issue_ids)
                + int(target.execute("SELECT COUNT(*) FROM materials").fetchone()[0]),
            ),
        )
        target.execute(
            "INSERT INTO application_compatibility VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                BOOTSTRAP_COMPATIBILITY_ID,
                "1.0.0",
                "1.0.0",
                "1.0.0",
                "1.0.0",
                "1.0.0",
                CONTRACT_VERSION,
                sqlite3.sqlite_version,
                imported_at,
            ),
        )
        rebuild_and_check_fts(target)
        after_hash = logical_state_hash(target)
        release_id = deterministic_id("release", source_sha256)
        target.execute(
            "INSERT INTO content_releases VALUES (?, 0, NULL, ?, 'daily', 1, ?, ?, ?, ?)",
            (
                release_id,
                deterministic_id("candidate", source_sha256),
                before_hash,
                after_hash,
                imported_at,
                imported_at,
            ),
        )
        target.commit()
    except BaseException:
        target.rollback()
        raise
    finally:
        source.close()
    if file_sha256(legacy_db) != source_sha256:
        raise LegacyImportError("Legacy database changed during read-only import")
    verify_database(target)
    target.commit()
    digest = database_digest(target)
    coverage = {
        table: CoverageRecord(
            source_name,
            derivation,
            allowed_empty,
            digest.table_counts[table],
            digest.table_hashes[table],
        )
        for table, (source_name, derivation, allowed_empty) in TABLE_COVERAGE.items()
    }
    unexplained = [
        table
        for table, record in coverage.items()
        if record.row_count == 0 and record.allowed_empty_evidence is None
    ]
    if unexplained:
        raise LegacyImportError(f"unexplained empty contract tables: {', '.join(unexplained)}")
    return ImportReport(
        source_sha256=source_sha256,
        evidence_manifest_sha256=manifest_sha256,
        inferred_published_issues=published,
        ambiguous_draft_issues=ambiguous,
        state_hash=digest.state_hash,
        coverage=coverage,
        digest=digest,
    )


__all__ = [
    "BootstrapSealedError",
    "CoverageRecord",
    "GazetteInput",
    "ImportReport",
    "LegacyImportError",
    "canonical_json",
    "content_hash",
    "deterministic_id",
    "import_legacy",
    "normalize_timestamp",
    "safe_relative_path",
    "strip_local_paths",
]
