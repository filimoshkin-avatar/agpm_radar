from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from radar_kx.identifiers import document_id
from radar_kx.issue_perimeter import load_perimeter_export

EXPORTER_PATH = Path(__file__).parents[1] / "scripts" / "export_v2_perimeter.py"

V2_SCHEMA = """
CREATE TABLE issues (
  issue_id TEXT PRIMARY KEY, issue_date TEXT NOT NULL UNIQUE, issue_number INTEGER,
  title TEXT NOT NULL, brief TEXT, lifecycle_status TEXT NOT NULL, published_at TEXT,
  publication_origin TEXT, empty_reason TEXT, content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE materials (
  material_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, canonical_url TEXT,
  source_name TEXT, published_at TEXT, publication_date_status TEXT NOT NULL, summary TEXT,
  agpm_takeaway TEXT, brief TEXT, content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE issue_materials (
  issue_id TEXT NOT NULL, material_id TEXT NOT NULL, sort_order INTEGER NOT NULL,
  perimeter TEXT NOT NULL, verdict TEXT NOT NULL, summary TEXT, agpm_takeaway TEXT,
  brief TEXT, theses_json TEXT NOT NULL, trend_notes TEXT, flags_json TEXT NOT NULL,
  key_material INTEGER NOT NULL, signal_score INTEGER, signal_strength TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (issue_id, material_id)
);
"""


def _load_exporter() -> Any:
    spec = importlib.util.spec_from_file_location("export_v2_perimeter", EXPORTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _v2_content_root(tmp_path: Path) -> Path:
    root = tmp_path / "content"
    (root / "releases").mkdir(parents=True)
    database_path = root / "releases" / "release.sqlite"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(V2_SCHEMA)
        connection.execute(
            "INSERT INTO issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "iss_1",
                "2026-08-14",
                61,
                "Радар 61",
                "issue brief",
                "published",
                "2026-08-14T09:00:00Z",
                "v2",
                None,
                "a" * 64,
                "2026-08-14T08:00:00Z",
                "2026-08-14T09:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO materials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "mat_1",
                "Material title",
                "https://example.com/a?utm_source=x",
                None,
                "Example",
                "2026-08-10T00:00:00Z",
                "resolved",
                "material summary",
                "material takeaway",
                "material brief",
                "b" * 64,
                "2026-08-10T00:00:00Z",
                "2026-08-10T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO issue_materials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "iss_1",
                "mat_1",
                0,
                "near",
                "core",
                None,
                "issue takeaway",
                None,
                json.dumps(["теза"], ensure_ascii=False),
                "trend notes",
                json.dumps({"paywall": True}),
                1,
                87,
                "strong",
                "2026-08-14T08:10:00Z",
                "2026-08-14T08:10:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    (root / "active.json").write_text(
        json.dumps(
            {
                "database": "releases/release.sqlite",
                "releaseId": "rel_test",
                "stateHash": "c" * 64,
            }
        ),
        encoding="utf-8",
    )
    return root


def test_export_preserves_every_source_column_and_loads_into_kx(tmp_path: Path) -> None:
    root = _v2_content_root(tmp_path)
    output = tmp_path / "perimeter.json"
    result = _load_exporter().export(content_root=root, output=output)

    assert result["memberRows"] == 1
    assert result["issues"] == 1 and result["materials"] == 1
    member = json.loads(output.read_text(encoding="utf-8"))["members"][0]
    for table, columns in (
        ("issue", ("issue_id", "issue_date", "issue_number", "title", "brief", "content_hash")),
        ("material", ("material_id", "title", "url", "canonical_url", "source_name")),
        ("issue_material", ("theses_json", "flags_json", "signal_score", "created_at")),
    ):
        for column in columns:
            assert column in member[table]

    export = load_perimeter_export(output)
    assert export.source.source_kind == "v2_content_release"
    assert export.source.perimeter_source_id == "v2_content_release:rel_test"
    assert len(export.source.source_sha256) == 64
    loaded = export.members[0]
    assert loaded.issue_number == 61
    assert loaded.issue_title == "Радар 61"
    assert loaded.perimeter == "near" and loaded.verdict == "core"
    assert loaded.key_material is True
    assert loaded.signal_score == 87 and loaded.signal_strength == "strong"
    assert loaded.theses == ["теза"] and loaded.flags == {"paywall": True}
    assert loaded.published_raw == "2026-08-10T00:00:00Z"
    # Tracking parameters are stripped by the shared canonical URL policy, and the
    # document id is derived from that canonical URL so KX de-duplicates correctly.
    assert loaded.canonical_url == "https://example.com/a"
    assert loaded.source_url == "https://example.com/a?utm_source=x"
    assert loaded.document_id == document_id("https://example.com/a")


def test_issue_level_editorial_text_overrides_material_text(tmp_path: Path) -> None:
    root = _v2_content_root(tmp_path)
    output = tmp_path / "perimeter.json"
    _load_exporter().export(content_root=root, output=output)
    member = load_perimeter_export(output).members[0]

    assert member.agpm_takeaway == "issue takeaway"
    assert member.summary == "material summary"
    assert member.brief == "material brief"
    assert member.trend_notes == "trend notes"
    assert member.payload["issue_material"]["summary"] is None


def _minimal_export() -> dict[str, Any]:
    return {
        "source": {
            "perimeter_source_id": "v2_content_release:rel_test",
            "source_kind": "v2_content_release",
            "source_reference": "releases/release.sqlite",
            "source_sha256": "d" * 64,
            "captured_at": "2026-08-21T18:00:00+00:00",
        },
        "members": [
            {
                "issue_id": "iss_1",
                "issue_date": "2026-08-14",
                "issue_title": "Радар 61",
                "material_ref": "mat_1",
                "sort_order": 0,
                "perimeter": "near",
                "verdict": "core",
                "title": "Material title",
                "source_url": "https://example.com/a",
                "canonical_url": "https://example.com/a",
            }
        ],
    }


def _write(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "perimeter.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def test_minimal_export_loads(tmp_path: Path) -> None:
    export = load_perimeter_export(_write(tmp_path, _minimal_export()))
    assert export.document_ids == {document_id("https://example.com/a")}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc["source"].__setitem__("source_kind", "guess"), "invalid perimeter source"),
        (lambda doc: doc["source"].__setitem__("source_sha256", "short"), "SHA-256"),
        (lambda doc: doc["members"][0].__setitem__("perimeter", "outer"), "invalid perimeter"),
        (lambda doc: doc["members"][0].__setitem__("verdict", "maybe"), "invalid verdict"),
        (lambda doc: doc["members"][0].__setitem__("material_ref", ""), "requires issue_id"),
        (lambda doc: doc["members"].append(dict(doc["members"][0])), "duplicate perimeter member"),
        (lambda doc: doc.__setitem__("members", []), "empty"),
    ],
)
def test_rejects_unusable_export(
    tmp_path: Path,
    mutate: Any,
    message: str,
) -> None:
    document = _minimal_export()
    mutate(document)
    with pytest.raises(ValueError, match=message):
        load_perimeter_export(_write(tmp_path, document))
