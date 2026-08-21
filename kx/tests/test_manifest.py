from __future__ import annotations

import json
from pathlib import Path

import pytest

from radar_kx.identifiers import document_id
from radar_kx.manifest import load_manifest


def _write_manifest(path: Path, values: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
        encoding="utf-8",
    )


def test_load_manifest_preserves_payload_and_normalizes_url(tmp_path: Path) -> None:
    path = tmp_path / "materials.jsonl"
    _write_manifest(
        path,
        [
            {
                "id": "material-1",
                "url": "https://example.com/a?utm_source=test",
                "canonical_url": "https://example.com/a",
                "title": "Заголовок",
                "first_seen_at": "2026-08-21T12:00:00+00:00",
            }
        ],
    )
    manifest = load_manifest(path)
    assert len(manifest.records) == 1
    record = manifest.records[0]
    assert record.canonical_url == "https://example.com/a"
    assert record.document_id == document_id(record.canonical_url)
    assert record.payload["title"] == "Заголовок"


def test_load_manifest_rejects_duplicate_material_ids(tmp_path: Path) -> None:
    path = tmp_path / "materials.jsonl"
    _write_manifest(
        path,
        [
            {"id": "same", "url": "https://example.com/a"},
            {"id": "same", "url": "https://example.com/b"},
        ],
    )
    with pytest.raises(ValueError, match="duplicate material id"):
        load_manifest(path)
