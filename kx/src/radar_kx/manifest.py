from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from radar_kx.identifiers import document_id, sha256_bytes, stable_json_bytes
from radar_kx.url_policy import normalize_url


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    material_id: str
    document_id: str
    source_url: str
    canonical_url: str
    title: str
    summary: str
    raw_excerpt: str
    perimeter: str | None
    published_raw: str | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    payload: dict[str, Any]
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class Manifest:
    source_sha256: str
    records: tuple[ManifestRecord, ...]


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def load_manifest(path: Path) -> Manifest:
    payload = path.read_bytes()
    records: list[ManifestRecord] = []
    seen_material_ids: set[str] = set()
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        if not raw_line.strip():
            continue
        value = json.loads(raw_line)
        if not isinstance(value, dict):
            raise ValueError(f"manifest line {line_number} must be an object")
        material_id = value.get("id")
        source_url = value.get("url")
        if not isinstance(material_id, str) or not material_id:
            raise ValueError(f"manifest line {line_number} has no material id")
        if material_id in seen_material_ids:
            raise ValueError(f"duplicate material id: {material_id}")
        seen_material_ids.add(material_id)
        if not isinstance(source_url, str) or not source_url:
            raise ValueError(f"manifest line {line_number} has no URL")
        candidate = value.get("canonical_url")
        canonical_url = normalize_url(candidate if isinstance(candidate, str) else source_url)
        records.append(
            ManifestRecord(
                material_id=material_id,
                document_id=document_id(canonical_url),
                source_url=source_url,
                canonical_url=canonical_url,
                title=str(value.get("title") or ""),
                summary=str(value.get("summary") or ""),
                raw_excerpt=str(value.get("raw_excerpt") or ""),
                perimeter=_optional_string(value.get("perimeter")),
                published_raw=_optional_string(value.get("published_at")),
                first_seen_at=_optional_datetime(value.get("first_seen_at")),
                last_seen_at=_optional_datetime(value.get("last_seen_at")),
                payload=value,
                payload_sha256=sha256_bytes(stable_json_bytes(value)),
            )
        )
    if not records:
        raise ValueError("manifest is empty")
    return Manifest(source_sha256=sha256_bytes(payload), records=tuple(records))
