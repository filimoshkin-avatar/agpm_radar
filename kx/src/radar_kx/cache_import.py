from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from radar_kx.database import Database
from radar_kx.identifiers import canonicalize_text
from radar_kx.parser import parse_content


@dataclass(frozen=True, slots=True)
class CacheImportResult:
    snapshot_files: int
    snapshot_versions: int
    truncated_files: int
    truncated_versions: int
    failures: tuple[str, ...]


def _datetime(value: object, fallback_path: Path) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.fromtimestamp(fallback_path.stat().st_mtime, tz=UTC)


def import_caches(
    database: Database,
    *,
    metadata_dir: Path,
    fulltext_dir: Path,
) -> CacheImportResult:
    failures: list[str] = []
    snapshot_files = 0
    snapshot_versions = 0
    for metadata_path in sorted(metadata_dir.glob("*.json")):
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("metadata is not an object")
            raw_snapshot = value.get("snapshot_path")
            snapshot_name = (
                Path(raw_snapshot).name
                if isinstance(raw_snapshot, str) and raw_snapshot
                else metadata_path.with_suffix(".html").name
            )
            snapshot_path = metadata_dir / snapshot_name
            if not snapshot_path.is_file():
                continue
            snapshot_files += 1
            source_url = value.get("canonical_url") or value.get("url")
            if not isinstance(source_url, str) or not source_url:
                raise ValueError("metadata has no URL")
            body = snapshot_path.read_bytes()
            content_type = str(value.get("content_type") or "text/html")
            parsed = parse_content(
                body=body,
                content_type=content_type,
                source_url=source_url,
                min_text_chars=database.settings.min_text_chars,
            )
            version = database.store_cached_version(
                source_url=source_url,
                body=body,
                parsed=parsed,
                source_kind="legacy_snapshot",
                fetched_at=_datetime(value.get("fetched_at"), snapshot_path),
                content_type=content_type,
                error_detail=None if parsed.is_complete else f"quality={parsed.quality}",
            )
            if version is not None:
                snapshot_versions += 1
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{metadata_path.name}: {exc}")

    truncated_files = 0
    truncated_versions = 0
    for fulltext_path in sorted(fulltext_dir.glob("*.json")):
        try:
            value = json.loads(fulltext_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("fulltext cache is not an object")
            source_url = value.get("canonical_url") or value.get("url")
            text = value.get("text")
            if not isinstance(source_url, str) or not source_url:
                raise ValueError("fulltext cache has no URL")
            if not isinstance(text, str) or not text.strip():
                continue
            truncated_files += 1
            canonical_text = canonicalize_text(text)
            base = parse_content(
                body=canonical_text.encode("utf-8"),
                content_type="text/plain; charset=utf-8",
                source_url=source_url,
                min_text_chars=database.settings.min_text_chars,
            )
            parsed = replace(
                base,
                text=canonical_text,
                title="",
                quality="legacy_truncated_20000",
                is_complete=False,
            )
            raw = fulltext_path.read_bytes()
            version = database.store_cached_version(
                source_url=source_url,
                body=raw,
                parsed=parsed,
                source_kind="legacy_truncated",
                fetched_at=_datetime(value.get("fetched_at"), fulltext_path),
                content_type="application/json",
                error_detail="legacy cache is capped at 20000 characters",
            )
            if version is not None:
                truncated_versions += 1
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{fulltext_path.name}: {exc}")

    return CacheImportResult(
        snapshot_files=snapshot_files,
        snapshot_versions=snapshot_versions,
        truncated_files=truncated_files,
        truncated_versions=truncated_versions,
        failures=tuple(failures),
    )
