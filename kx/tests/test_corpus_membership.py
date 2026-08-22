from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from radar_kx.corpus_membership import (
    V2_PUBLICATION_WINDOW_DAYS,
    LegacyMaterial,
    build_report,
    fulltext_cache_key,
    load_discovery,
    load_fulltext,
    load_legacy,
    load_legacy_source_metadata_urls,
    load_v2_release,
    v2_material_id,
    within_v2_publication_window,
)
from radar_kx.identifiers import document_id
from radar_kx.url_policy import normalize_url

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
V2_IMPORTER = REPOSITORY_ROOT / "v2" / "packages" / "legacy_bridge" / "importer.py"
V2_DAILY_BUILDER = REPOSITORY_ROOT / "v2" / "tools" / "build_stage14_daily.py"


def _legacy(**overrides: Any) -> LegacyMaterial:
    defaults: dict[str, Any] = {
        "legacy_id": "0123456789abcdef",
        "canonical_url": "https://example.com/a",
        "url": "https://www.example.com/a/",
        "issue_date": "2026-08-20",
        "title": "A",
        "published_at": "2026-08-19",
        "publication_date_status": "resolved",
    }
    defaults.update(overrides)
    return LegacyMaterial(**defaults)


def test_v2_material_id_reproduces_the_importer_derivation() -> None:
    # Two production ids taken from the active content release: the historical duplicate
    # that the 2026-06-09 correction removed, and the one it kept.
    assert v2_material_id("6b8e09387c2aefc8") == "mat_fe9379c0920ff2782cf6ebb3"
    assert v2_material_id("f16e72ab343c7b8e") == "mat_47658d588f512751040075f7"


def test_v2_material_id_formula_still_matches_the_importer_source() -> None:
    if not V2_IMPORTER.exists():
        pytest.skip("Radar V2 tree is not present next to this checkout")
    source = V2_IMPORTER.read_text(encoding="utf-8")
    assert 'f"radar-v2:{namespace}:{legacy_key}".encode()' in source
    assert ".hexdigest()[:24]" in source
    # The two namespaces the reconciliation reproduces by hand.
    assert 'legacy_key=f"source-metadata:{metadata_url}"' in source
    assert '"material": "mat"' in source


def test_publication_window_constant_still_matches_the_daily_builder() -> None:
    if not V2_DAILY_BUILDER.exists():
        pytest.skip("Radar V2 tree is not present next to this checkout")
    source = V2_DAILY_BUILDER.read_text(encoding="utf-8")
    assert f"timedelta(days={V2_PUBLICATION_WINDOW_DAYS})" in source
    assert "earliest <= published_day <= issue_day" in source


@pytest.mark.parametrize(
    ("published_at", "status", "expected"),
    [
        (None, "unresolved", True),
        ("2026-08-19", "resolved", True),
        ("2026-08-20", "resolved", True),
        ("2026-07-21", "resolved", True),
        ("2026-07-20", "resolved", False),
        ("2026-08-21", "resolved", False),
        ("2024-10-29", "resolved", False),
        (None, "resolved", False),
        ("2026-08-19", "unresolved", False),
    ],
)
def test_publication_window_matches_the_v2_daily_rule(
    published_at: str | None, status: str, expected: bool
) -> None:
    material = _legacy(published_at=published_at, publication_date_status=status)
    assert within_v2_publication_window(material) is expected


def test_fulltext_cache_key_is_a_prefix_of_the_kx_document_id() -> None:
    # The Project Manager names an extracted-text file by sha256(canonical_url)[:24] and KX
    # keys a document by sha256(normalize_url(canonical_url)). Where normalization changes
    # nothing the file name is literally a prefix of the document id, which is what lets the
    # two stores be reconciled without a lookup table.
    url = "https://appian.com/learn/topics/enterprise-ai/ai-agent-use-cases"
    assert document_id(normalize_url(url)).startswith(fulltext_cache_key(url))


def _write_legacy(path: Path, materials: list[dict[str, Any]], metadata_urls: list[str]) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE materials (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, canonical_url TEXT,
          radar_issue_date TEXT, published_at TEXT,
          publication_date_status TEXT NOT NULL DEFAULT 'unresolved'
        );
        CREATE TABLE source_metadata (url TEXT PRIMARY KEY);
        """
    )
    connection.executemany(
        "INSERT INTO materials VALUES (:id, :title, :url, :canonical_url,"
        " :radar_issue_date, :published_at, :publication_date_status)",
        materials,
    )
    connection.executemany(
        "INSERT INTO source_metadata VALUES (?)", [(url,) for url in metadata_urls]
    )
    connection.commit()
    connection.close()


def _write_release(
    path: Path,
    materials: list[dict[str, Any]],
    selections: list[tuple[str, str, int]],
    deferred: list[str],
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE issues (issue_id TEXT PRIMARY KEY, issue_date TEXT NOT NULL);
        CREATE TABLE materials (
          material_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
          canonical_url TEXT
        );
        CREATE TABLE issue_materials (
          issue_id TEXT NOT NULL, material_id TEXT NOT NULL, sort_order INTEGER NOT NULL,
          PRIMARY KEY (issue_id, material_id)
        );
        CREATE TABLE editorial_queue (
          queue_id TEXT PRIMARY KEY, material_id TEXT NOT NULL, state TEXT NOT NULL
        );
        """
    )
    connection.executemany(
        "INSERT INTO materials VALUES (:material_id, :title, :url, :canonical_url)", materials
    )
    for issue_date in sorted({item[0] for item in selections}):
        connection.execute("INSERT INTO issues VALUES (?, ?)", (f"iss_{issue_date}", issue_date))
    connection.executemany(
        "INSERT INTO issue_materials VALUES (?, ?, ?)",
        [
            (f"iss_{issue_date}", material_id, order)
            for issue_date, material_id, order in selections
        ],
    )
    connection.executemany(
        "INSERT INTO editorial_queue VALUES (?, ?, 'deferred')",
        [(f"que_{index}", material_id) for index, material_id in enumerate(deferred)],
    )
    connection.commit()
    connection.close()


def _kx_extract(canonical_urls: list[str], *, complete: set[str] | None = None) -> dict[str, Any]:
    complete = canonical_urls_set = set(canonical_urls) if complete is None else complete
    index = [
        {
            "documentId": document_id(normalize_url(url)),
            "canonicalUrl": url,
            "hasCompleteVersion": url in complete,
            "hasMaterial": True,
        }
        for url in sorted(canonical_urls_set | set(canonical_urls))
    ]
    members = [
        {
            "perimeterSourceId": "v2_content_release:rel_test",
            "issueId": "iss",
            "issueDate": "2026-08-20",
            "materialRef": f"mat_{position}",
            "documentId": document_id(normalize_url(url)),
            "canonicalUrl": url,
        }
        for position, url in enumerate(canonical_urls)
    ]
    documents = [dict(item) for item in index]
    return {
        "generatedAt": "2026-08-22T00:00:00Z",
        "schemaVersion": 2,
        "counts": {
            "documents": len(index),
            "documentsWithoutMaterial": 0,
            "sourceMaterials": len(index),
            "materialDocuments": len(index),
            "materialDocumentsDistinctDocuments": len(index),
            "documentVersions": len(index),
            "documentVersionsComplete": len(complete),
            "documentsWithCompleteVersion": len(complete),
            "chunks": len(index),
            "perimeterSources": 1,
            "perimeterMembers": len(members),
            "perimeterDocumentsUnion": len({item["documentId"] for item in members}),
        },
        "corpusImports": [
            {
                "corpusSha256": "0" * 64,
                "sourceName": "materials.jsonl",
                "rowCount": 2,
                "documentCount": 2,
                "importedAt": "2026-08-21T00:00:00Z",
            }
        ],
        "perimeterSources": [
            {
                "perimeterSourceId": "v2_content_release:rel_test",
                "sourceKind": "v2_content_release",
                "sourceReference": "releases/test.sqlite",
                "sourceSha256": "1" * 64,
                "capturedAt": "2026-08-22T00:00:00Z",
                "members": len(members),
                "documents": len({item["documentId"] for item in members}),
            }
        ],
        "perimeterMembers": members,
        "perimeterDocuments": documents,
        "documentIndex": index,
    }


@pytest.fixture
def corpus(tmp_path: Path) -> dict[str, Any]:
    """A miniature of the production corpus: a repeat carry, a stale date and a metadata row."""
    kept = {
        "id": "aaaa000000000001",
        "title": "Carried",
        "url": "https://www.example.com/carried/",
        "canonical_url": "https://example.com/carried",
        "radar_issue_date": "2026-08-19",
        "published_at": "2026-08-19",
        "publication_date_status": "resolved",
    }
    repeat = {**kept, "id": "aaaa000000000002", "radar_issue_date": "2026-08-20"}
    stale = {
        "id": "aaaa000000000003",
        "title": "Stale",
        "url": "https://www.example.com/stale",
        "canonical_url": "https://example.com/stale",
        "radar_issue_date": "2026-08-20",
        "published_at": "2024-10-29",
        "publication_date_status": "resolved",
    }
    fresh = {
        "id": "aaaa000000000004",
        "title": "Fresh",
        "url": "https://www.example.com/fresh",
        "canonical_url": "https://example.com/fresh",
        "radar_issue_date": "2026-08-20",
        "published_at": "2026-08-20",
        "publication_date_status": "resolved",
    }
    metadata_url = "https://example.com/metadata-only"
    legacy_db = tmp_path / "radar.sqlite"
    _write_legacy(legacy_db, [kept, repeat, stale, fresh], [metadata_url])

    release_db = tmp_path / "release.sqlite"
    deferred_id = v2_material_id("deferred:zzzz")
    _write_release(
        release_db,
        [
            {
                "material_id": v2_material_id(str(kept["id"])),
                "title": "Carried",
                "url": kept["url"],
                "canonical_url": kept["canonical_url"],
            },
            {
                "material_id": v2_material_id(str(repeat["id"])),
                "title": "Carried",
                "url": repeat["url"],
                "canonical_url": repeat["canonical_url"],
            },
            {
                "material_id": v2_material_id(str(fresh["id"])),
                "title": "Fresh",
                "url": fresh["url"],
                "canonical_url": fresh["canonical_url"],
            },
            {
                "material_id": v2_material_id(f"source-metadata:{metadata_url}"),
                "title": "Metadata only",
                "url": metadata_url,
                "canonical_url": metadata_url,
            },
            {
                "material_id": deferred_id,
                "title": "Deferred",
                "url": "https://example.com/deferred",
                "canonical_url": "https://example.com/deferred",
            },
        ],
        [
            ("2026-08-19", v2_material_id(str(kept["id"])), 0),
            ("2026-08-20", v2_material_id(str(fresh["id"])), 0),
        ],
        [deferred_id],
    )

    registry = tmp_path / "materials.jsonl"
    registry.write_text(
        "\n".join(
            json.dumps({"id": f"bbbb00000000000{index}", "canonical_url": url, "perimeter": "near"})
            for index, url in enumerate(
                ["https://example.com/carried", "https://example.com/fresh"]
            )
        )
        + "\n",
        encoding="utf-8",
    )
    fulltext_dir = tmp_path / "source-fulltext"
    fulltext_dir.mkdir()
    for url in ("https://example.com/carried", "https://example.com/fresh"):
        (fulltext_dir / f"{fulltext_cache_key(url)}.json").write_text(
            json.dumps({"canonical_url": url, "status": "resolved", "text": "body"}),
            encoding="utf-8",
        )
    return {
        "legacy_db": legacy_db,
        "release_db": release_db,
        "registry": registry,
        "fulltext_dir": fulltext_dir,
        "perimeter_urls": ["https://example.com/carried", "https://example.com/fresh"],
    }


def _report(corpus: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    kx = overrides.pop("kx", _kx_extract(list(corpus["perimeter_urls"])))
    return build_report(
        legacy=overrides.pop("legacy", load_legacy(corpus["legacy_db"])),
        legacy_metadata_urls=load_legacy_source_metadata_urls(corpus["legacy_db"]),
        release=load_v2_release(corpus["release_db"]),
        discovery=load_discovery(corpus["registry"]),
        fulltext=load_fulltext(corpus["fulltext_dir"]),
        kx=kx,
        inputs={"legacyDb": str(corpus["legacy_db"])},
    )


def test_every_layer_transition_is_explained_on_a_healthy_corpus(corpus: dict[str, Any]) -> None:
    report = _report(corpus)
    assert report["status"] == "ok", [c for c in report["checks"] if not c["ok"]]
    layers = report["layers"]
    assert layers["legacy"]["rows"] == 4
    assert layers["legacy"]["distinctCanonicalUrls"] == 3
    assert layers["v2Release"]["materials"] == 5
    assert layers["v2Release"]["materialsByOrigin"] == {
        "legacy_deferred_queue": 1,
        "legacy_material": 3,
        "legacy_source_metadata": 1,
    }
    assert layers["v2Release"]["selectionRows"] == 2
    transition = report["transitions"]["legacyToV2Selection"]
    assert [item["canonicalUrl"] for item in transition["droppedOutsidePublicationWindow"]] == [
        "https://example.com/stale"
    ]
    assert [item["issueDate"] for item in transition["droppedAsHistoricalDuplicate"]] == [
        "2026-08-20"
    ]
    assert transition["droppedUnexplained"] == []


def test_a_dropped_row_with_no_recorded_reason_fails_the_gate(corpus: dict[str, Any]) -> None:
    # A Legacy row that is inside the window, is not a repeat carry, and is still missing
    # from the selection is real drift between the contours - the report must refuse it.
    legacy = [
        *load_legacy(corpus["legacy_db"]),
        _legacy(legacy_id="aaaa000000000009", canonical_url="https://example.com/orphan"),
    ]
    report = _report(corpus, legacy=legacy)
    assert report["status"] == "failed"
    unexplained = report["transitions"]["legacyToV2Selection"]["droppedUnexplained"]
    assert [item["canonicalUrl"] for item in unexplained] == ["https://example.com/orphan"]


def test_a_perimeter_document_without_full_text_fails_the_gate(corpus: dict[str, Any]) -> None:
    kx = _kx_extract(list(corpus["perimeter_urls"]), complete={"https://example.com/carried"})
    report = _report(corpus, kx=kx)
    assert report["status"] == "failed"
    failed = {check["name"] for check in report["checks"] if not check["ok"]}
    assert "kx.perimeter_full_text_is_complete" in failed
    missing = report["layers"]["kx"]["perimeterDocumentsMissingFullText"]
    assert [item["canonicalUrl"] for item in missing] == ["https://example.com/fresh"]


def test_a_perimeter_that_is_not_the_active_selection_fails_the_gate(
    corpus: dict[str, Any],
) -> None:
    # Equal counts over different documents: the set difference has to catch this, because
    # the cardinality check alone would pass.
    kx = _kx_extract(["https://example.com/carried", "https://example.com/other"])
    report = _report(corpus, kx=kx)
    assert report["status"] == "failed"
    failed = {check["name"] for check in report["checks"] if not check["ok"]}
    assert "kx.perimeter_document_ids_are_reproducible_from_the_release" in failed


def test_the_report_runs_without_a_kx_extract(corpus: dict[str, Any]) -> None:
    report = _report(corpus, kx=None)
    assert report["status"] == "ok"
    assert report["layers"]["kx"] is None
    assert "notInKx" not in report["layers"]["discovery"]


def test_file_stores_report_what_kx_does_not_have(corpus: dict[str, Any]) -> None:
    report = _report(corpus)
    discovery = report["layers"]["discovery"]
    # The registry snapshot KX imported is smaller than the file is now; the drift has to be
    # a number in the report, not something a reader is expected to notice.
    assert discovery["kxCorpusSnapshotRows"] == 2
    assert discovery["rowsAddedSinceKxSnapshot"] == 0
    assert discovery["legacyUrlsAbsentFromRegistry"] == ["https://example.com/stale"]
