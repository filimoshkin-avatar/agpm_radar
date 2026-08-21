from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from radar_kx.identifiers import document_id, sha256_bytes, stable_json_bytes
from radar_kx.url_policy import normalize_url

SOURCE_KINDS = frozenset({"v2_content_release", "legacy_radar_db"})
PERIMETERS = frozenset({"near", "mid", "far"})
VERDICTS = frozenset({"core", "adjacent"})


@dataclass(frozen=True, slots=True)
class PerimeterSource:
    perimeter_source_id: str
    source_kind: str
    source_reference: str
    source_sha256: str
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class PerimeterMember:
    issue_id: str
    material_ref: str
    document_id: str
    issue_date: date
    issue_number: int | None
    issue_title: str
    sort_order: int
    perimeter: str
    verdict: str | None
    key_material: bool
    signal_score: int | None
    signal_strength: str | None
    title: str
    source_url: str
    canonical_url: str
    summary: str | None
    agpm_takeaway: str | None
    brief: str | None
    trend_notes: str | None
    theses: list[Any]
    flags: dict[str, Any]
    published_raw: str | None
    payload: dict[str, Any]
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class PerimeterExport:
    source: PerimeterSource
    members: tuple[PerimeterMember, ...]

    @property
    def document_ids(self) -> frozenset[str]:
        return frozenset(member.document_id for member in self.members)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _member(value: dict[str, Any]) -> PerimeterMember:
    issue_id = _text(value.get("issue_id"))
    material_ref = _text(value.get("material_ref"))
    if not issue_id or not material_ref:
        raise ValueError("perimeter member requires issue_id and material_ref")
    source_url = _text(value.get("source_url"))
    canonical_url = normalize_url(_text(value.get("canonical_url")) or source_url)
    perimeter = _text(value.get("perimeter"))
    if perimeter not in PERIMETERS:
        raise ValueError(f"invalid perimeter: {perimeter!r}")
    verdict = _optional_text(value.get("verdict"))
    if verdict is not None and verdict not in VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    theses = value.get("theses")
    flags = value.get("flags")
    return PerimeterMember(
        issue_id=issue_id,
        material_ref=material_ref,
        document_id=document_id(canonical_url),
        issue_date=date.fromisoformat(_text(value.get("issue_date"))),
        issue_number=_optional_int(value.get("issue_number")),
        issue_title=_text(value.get("issue_title")),
        sort_order=int(value.get("sort_order", 0)),
        perimeter=perimeter,
        verdict=verdict,
        key_material=bool(value.get("key_material")),
        signal_score=_optional_int(value.get("signal_score")),
        signal_strength=_optional_text(value.get("signal_strength")),
        title=_text(value.get("title")),
        source_url=source_url or canonical_url,
        canonical_url=canonical_url,
        summary=_optional_text(value.get("summary")),
        agpm_takeaway=_optional_text(value.get("agpm_takeaway")),
        brief=_optional_text(value.get("brief")),
        trend_notes=_optional_text(value.get("trend_notes")),
        theses=theses if isinstance(theses, list) else [],
        flags=flags if isinstance(flags, dict) else {},
        published_raw=_optional_text(value.get("published_at")),
        payload=value,
        payload_sha256=sha256_bytes(stable_json_bytes(value)),
    )


def load_perimeter_export(path: Path) -> PerimeterExport:
    document = json.loads(path.read_bytes())
    if not isinstance(document, dict):
        raise ValueError("perimeter export must be a JSON object")
    raw_source = document.get("source")
    raw_members = document.get("members")
    if not isinstance(raw_source, dict) or not isinstance(raw_members, list):
        raise ValueError("perimeter export requires a source object and a members array")
    source_kind = _text(raw_source.get("source_kind"))
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"invalid perimeter source kind: {source_kind!r}")
    source = PerimeterSource(
        perimeter_source_id=_text(raw_source.get("perimeter_source_id")),
        source_kind=source_kind,
        source_reference=_text(raw_source.get("source_reference")),
        source_sha256=_text(raw_source.get("source_sha256")),
        captured_at=datetime.fromisoformat(_text(raw_source.get("captured_at"))),
    )
    if not source.perimeter_source_id or not source.source_reference:
        raise ValueError("perimeter source requires an id and a reference")
    if len(source.source_sha256) != 64:
        raise ValueError("perimeter source requires the SHA-256 of the source artifact")
    members: list[PerimeterMember] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw_members:
        if not isinstance(entry, dict):
            raise ValueError("perimeter member must be an object")
        member = _member(entry)
        key = (member.issue_id, member.material_ref)
        if key in seen:
            raise ValueError(f"duplicate perimeter member: {key}")
        seen.add(key)
        members.append(member)
    if not members:
        raise ValueError("perimeter export is empty")
    return PerimeterExport(source=source, members=tuple(members))
