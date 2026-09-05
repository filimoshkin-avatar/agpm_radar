"""Synthetic-first Stage 3 schema, bootstrap importer and equivalence regressions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]
from packages.legacy_bridge.importer import (
    BootstrapSealedError,
    GazetteInput,
    ImportReport,
    import_legacy,
)
from packages.storage.hashing import REPLICATED_TABLES, database_digest
from packages.storage.migrations import MigrationError, apply_migrations, create_database
from tools.compare_databases import compare

ROOT = Path(__file__).resolve().parents[2]
V2_ROOT = ROOT / "v2"
IMPORTED_AT = "2026-01-03T12:00:00Z"

LEGACY_SCHEMA = """
CREATE TABLE issues (
 issue_date TEXT PRIMARY KEY, issue_number INTEGER, title TEXT, brief TEXT,
 theses_json TEXT NOT NULL, report_md_path TEXT, report_docx_path TEXT,
 status TEXT NOT NULL, published_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE materials (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, canonical_url TEXT,
 source_name TEXT, source_id TEXT, published_at TEXT, first_seen_at TEXT,
 radar_issue_date TEXT, publication_date_source TEXT, publication_date_confidence REAL,
 publication_date_status TEXT NOT NULL, perimeter TEXT, verdict TEXT, summary TEXT,
 agpm_takeaway TEXT, governance_flag INTEGER NOT NULL, security_flag INTEGER NOT NULL,
 human_in_the_loop_flag INTEGER NOT NULL, pmo_flag INTEGER NOT NULL, isup_flag INTEGER NOT NULL,
 mcp_flag INTEGER NOT NULL, key_material INTEGER NOT NULL, docx_source_path TEXT,
 md_source_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 brief TEXT, theses_json TEXT NOT NULL, trend_notes TEXT, signal_score INTEGER,
 signal_strength TEXT NOT NULL
);
CREATE TABLE sources (id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT);
CREATE TABLE source_metadata (
 url TEXT PRIMARY KEY, canonical_url TEXT, title TEXT, extracted_published_at TEXT,
 extraction_source TEXT, confidence REAL, status TEXT, fetched_at TEXT, http_status INTEGER,
 content_type TEXT, snapshot_path TEXT, error TEXT
);
CREATE TABLE rubrics (id TEXT PRIMARY KEY, title TEXT NOT NULL, sort_order INTEGER NOT NULL);
CREATE TABLE material_rubrics (
 material_id TEXT NOT NULL, rubric_id TEXT NOT NULL, confidence REAL, source TEXT,
 PRIMARY KEY(material_id, rubric_id)
);
CREATE TABLE daily_stats (
 stat_date TEXT PRIMARY KEY, viewed INTEGER, included INTEGER, cut INTEGER, near INTEGER,
 mid INTEGER, far INTEGER, core INTEGER, adjacent INTEGER, updated_at TEXT
);
CREATE TABLE llm_classifications (
 id INTEGER PRIMARY KEY, material_id TEXT, provider TEXT, model TEXT, prompt_version TEXT,
 request_path TEXT, response_path TEXT, normalized_json TEXT, confidence REAL,
 status TEXT, error TEXT, created_at TEXT
);
CREATE TABLE source_domain_rules (
 host TEXT PRIMARY KEY, date_strategy TEXT, notes TEXT, updated_at TEXT
);
CREATE TABLE material_date_quality (
 material_id TEXT PRIMARY KEY, source_host TEXT, publication_date_status TEXT,
 issue_date_delta_days INTEGER, severity TEXT, review_status TEXT, reason TEXT,
 diagnostic_json TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE issue_daily_analysis (
 issue_date TEXT PRIMARY KEY, headline TEXT, analysis_json TEXT, provider TEXT, model TEXT,
 prompt_version TEXT, request_path TEXT, response_path TEXT, status TEXT, error TEXT,
 created_at TEXT, updated_at TEXT
);
CREATE TABLE issue_llm_theses (
 issue_date TEXT PRIMARY KEY, theses_json TEXT, brief TEXT, provider TEXT, model TEXT,
 prompt_version TEXT, request_path TEXT, response_path TEXT, status TEXT, error TEXT,
 created_at TEXT, updated_at TEXT
);
CREATE TABLE material_llm_summaries (
 material_id TEXT PRIMARY KEY, issue_date TEXT, short_text TEXT, agpm_angle TEXT,
 provider TEXT, model TEXT, prompt_version TEXT, request_path TEXT, response_path TEXT,
 status TEXT, error TEXT, created_at TEXT, updated_at TEXT
);
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_synthetic_legacy(tmp_path: Path) -> tuple[Path, Path, str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "synthetic-legacy.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(LEGACY_SCHEMA)
        issues = [
            ("2026-01-01", 1, "Public synthetic", "safe /root/private/file removed"),
            ("2026-01-02", 2, "Draft synthetic", "not allowlisted"),
        ]
        for issue_date, number, title, brief in issues:
            connection.execute(
                "INSERT INTO issues VALUES (?, ?, ?, ?, '[]', '/root/report', '/mnt/report', 'draft', NULL, ?, ?)",
                (
                    issue_date,
                    number,
                    title,
                    brief,
                    f"{issue_date} 08:00:00",
                    f"{issue_date} 09:00:00",
                ),
            )
        connection.execute(
            "INSERT INTO sources VALUES ('synthetic-source', 'Synthetic Source', 'https://example.test')"
        )
        for index, issue_date in enumerate(("2026-01-01", "2026-01-02"), start=1):
            material_id = f"legacy-material-{index}"
            url = f"https://example.test/article-{index}"
            connection.execute(
                """
                INSERT INTO materials VALUES (
                  ?, ?, ?, ?, 'Synthetic Source', 'synthetic-source', ?, ?, ?, 'metadata', 1.0,
                  'resolved', 'near', 'core', ?, 'Takeaway', 1, 0, 0, 1, 0, 0, 1,
                  '/root/raw.docx', '/mnt/raw.md', ?, ?, 'Brief', '["Thesis"]', 'Trend', 10, 'strong'
                )
                """,
                (
                    material_id,
                    f"Synthetic material {index}",
                    url,
                    url,
                    f"{issue_date} 07:00:00",
                    f"{issue_date} 06:00:00",
                    issue_date,
                    "Summary /srv/private",
                    f"{issue_date} 06:00:00",
                    f"{issue_date} 09:00:00",
                ),
            )
            connection.execute(
                "INSERT INTO daily_stats VALUES (?, 2, 1, 1, 1, 0, 0, 1, 0, ?)",
                (issue_date, f"{issue_date} 09:00:00"),
            )
            connection.execute(
                "INSERT INTO issue_daily_analysis VALUES (?, 'Headline', '{}', 'fallback', 'rules-daily-analysis-v1', 'v1', '/root/request', '/root/response', 'fallback', NULL, ?, ?)",
                (issue_date, f"{issue_date} 08:30:00", f"{issue_date} 09:00:00"),
            )
        connection.execute(
            "INSERT INTO issue_llm_theses VALUES ('2026-01-01', '[\"LLM thesis\"]', 'LLM brief', 'synthetic', 'model', 'v1', '/root/request', '/root/response', 'success', NULL, '2026-01-01 08:30:00', '2026-01-01 09:00:00')"
        )
        connection.execute(
            "INSERT INTO rubrics VALUES ('synthetic-rubric', 'Synthetic rubric', 10)"
        )
        connection.execute(
            "INSERT INTO material_rubrics VALUES ('legacy-material-1', 'synthetic-rubric', 0.9, 'synthetic')"
        )
        connection.execute(
            "INSERT INTO source_domain_rules VALUES ('example.test', 'structured', 'safe', '2026-01-01 09:00:00')"
        )
        connection.execute(
            "INSERT INTO material_date_quality VALUES ('legacy-material-1', 'example.test', 'resolved', 0, 'ok', 'queued', 'manual check', '{}', '2026-01-01 08:00:00', '2026-01-01 09:00:00')"
        )
        connection.execute(
            "INSERT INTO source_metadata VALUES ('https://example.test/article-1', 'https://example.test/article-1', 'Synthetic material 1', '2026-01-01', 'synthetic', 1.0, 'resolved', '2026-01-01 08:00:00', 200, 'text/html', '/root/snapshot', NULL)"
        )
        connection.execute(
            "INSERT INTO llm_classifications VALUES (1, 'legacy-material-1', 'fallback', 'rules-v1', 'v1', '/root/request', '/root/response', '{}', 1.0, 'ok', NULL, '2026-01-01 08:00:00')"
        )
        connection.execute(
            "INSERT INTO material_llm_summaries VALUES ('legacy-material-1', '2026-01-01', 'Short', 'Angle', 'synthetic', 'model', 'v1', '/root/request', '/root/response', 'success', NULL, '2026-01-01 08:00:00', '2026-01-01 09:00:00')"
        )
    database_sha = _sha256(database)
    evidence_items = [
        {
            "kind": kind,
            "relativePath": f"synthetic/{kind}/2026-01-01",
            "sha256": hashlib.sha256(kind.encode()).hexdigest(),
        }
        for kind in ("canonical_report", "raw_docx", "normalized_json", "public_json")
    ]
    manifest_data = {
        "baselineDatabaseSha256": database_sha,
        "contractVersion": "synthetic-1",
        "issueCount": 1,
        "issues": [
            {
                "evidence": evidence_items,
                "issueDate": "2026-01-01",
                "issueNumber": 1,
                "legacyIssueRowSha256": hashlib.sha256(b"synthetic issue row").hexdigest(),
                "legacyPublishedAt": None,
                "legacyStatus": "draft",
                "materialCount": 1,
                "statsInvariantPassed": True,
            }
        ],
    }
    manifest = tmp_path / "synthetic-evidence.json"
    manifest.write_text(json.dumps(manifest_data, sort_keys=True), encoding="utf-8")
    gazette = tmp_path / "synthetic-gazette.html"
    gazette.write_text("<!doctype html><title>Synthetic Gazette</title>", encoding="utf-8")
    return database, manifest, _sha256(manifest), gazette


def _import_synthetic(tmp_path: Path, name: str = "target.sqlite") -> tuple[Path, ImportReport]:
    legacy, manifest, manifest_sha, gazette_asset = _create_synthetic_legacy(tmp_path)
    deferred = tmp_path / "synthetic-deferred.jsonl"
    deferred.write_text(
        json.dumps(
            {
                "id": "synthetic-deferred",
                "title": "Synthetic deferred material",
                "url": "https://example.test/deferred",
                "_radar_deferred": {"last_deferred_for": "2026-01-02"},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    target_path = tmp_path / name
    create_database(target_path, applied_at=IMPORTED_AT)
    with sqlite3.connect(target_path) as target:
        report = import_legacy(
            legacy_db=legacy,
            target=target,
            evidence_manifest=manifest,
            expected_manifest_sha256=manifest_sha,
            imported_at=IMPORTED_AT,
            deferred_queue=deferred,
            gazette=GazetteInput(
                path=gazette_asset,
                relative_path="gazettes/synthetic.html",
                period="2026-01",
                title="Synthetic Gazette",
                published_at="2026-01-03T00:00:00Z",
            ),
        )
    return target_path, report


def test_schema_matches_every_contract_table_column_fk_view_and_fts(tmp_path: Path) -> None:
    database = tmp_path / "schema.sqlite"
    create_database(database, applied_at=IMPORTED_AT)
    contract = cast(
        dict[str, object],
        yaml.safe_load((ROOT / "contracts/v1/sqlite-contract.yaml").read_text(encoding="utf-8")),
    )
    tables = cast(dict[str, dict[str, object]], contract["tables"])
    views = cast(dict[str, object], contract["publicViews"])
    with sqlite3.connect(database) as connection:
        assert set(tables) == set(REPLICATED_TABLES)
        for table_name, definition in tables.items():
            info = {
                str(row[1]): row for row in connection.execute(f'PRAGMA table_info("{table_name}")')
            }
            columns = cast(dict[str, dict[str, object]], definition["columns"])
            assert set(info) == set(columns), table_name
            for column_name, column_contract in columns.items():
                assert str(info[column_name][2]).upper() == str(column_contract["type"])
                if column_contract.get("nullable") is False:
                    assert bool(info[column_name][3]) or bool(info[column_name][5])
            expected_fks: set[tuple[tuple[str, ...], str, tuple[str, ...], str]] = set()
            for fk in cast(list[dict[str, object]], definition.get("foreignKeys", [])):
                expected_fks.add(
                    (
                        tuple(cast(list[str], fk["columns"])),
                        str(fk["references"]),
                        tuple(cast(list[str], fk["referencedColumns"])),
                        str(fk["onDelete"]),
                    )
                )
            actual_fk_rows = list(connection.execute(f'PRAGMA foreign_key_list("{table_name}")'))
            grouped: dict[int, list[sqlite3.Row | tuple[object, ...]]] = {}
            for row in actual_fk_rows:
                grouped.setdefault(int(str(row[0])), []).append(row)
            actual_fks = {
                (
                    tuple(str(row[3]) for row in sorted(rows, key=lambda item: int(str(item[1])))),
                    str(rows[0][2]),
                    tuple(str(row[4]) for row in sorted(rows, key=lambda item: int(str(item[1])))),
                    str(rows[0][6]),
                )
                for rows in grouped.values()
            }
            assert actual_fks == expected_fks, table_name
            expected_primary_key = tuple(cast(list[str], definition["primaryKey"]))
            actual_primary_key = tuple(
                str(row[1])
                for row in sorted(info.values(), key=lambda item: int(item[5]))
                if int(row[5]) > 0
            )
            assert actual_primary_key == expected_primary_key, table_name
            indexes = [
                tuple(
                    str(index_column[2])
                    for index_column in connection.execute(f'PRAGMA index_xinfo("{index_row[1]}")')
                    if int(index_column[5]) == 1
                )
                for index_row in connection.execute(f'PRAGMA index_list("{table_name}")')
            ]
            for unique_key in cast(list[list[str]], definition.get("uniqueKeys", [])):
                assert tuple(unique_key) in indexes, (table_name, unique_key)
            for fk_columns, _reference, _referenced_columns, _on_delete in expected_fks:
                assert any(index[: len(fk_columns)] == fk_columns for index in indexes), (
                    table_name,
                    fk_columns,
                )
        actual_views = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'view'")
        }
        assert actual_views == set(views)
        # Migration 0004 took the search index and its source view out: nothing
        # queried them, and the contract no longer names a derived table.
        assert not tuple(
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name LIKE 'published_materials_fts%'"
            )
        )
        migration_sql = (V2_ROOT / "packages/storage/migrations/0001_initial.sql").read_text()
        assert "AUTOINCREMENT" not in migration_sql
        assert "datetime('now')" not in migration_sql
        assert "random(" not in migration_sql


def test_synthetic_import_infers_publication_preserves_draft_and_seals(tmp_path: Path) -> None:
    target_path, report = _import_synthetic(tmp_path)
    assert report.inferred_published_issues == 1
    assert report.ambiguous_draft_issues == 1
    assert not [
        record
        for record in report.coverage.values()
        if record.row_count == 0 and record.allowed_empty_evidence is None
    ]
    with sqlite3.connect(target_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM issues WHERE lifecycle_status='published'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM issues WHERE lifecycle_status='draft'"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM pub_issues_v1").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM pub_issue_materials_v1").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT public_api_version FROM application_compatibility"
            ).fetchone()[0]
            == "1.0.0"
        )
        assert connection.execute("SELECT COUNT(*) FROM legacy_issue_provenance").fetchone()[0] == 2
        deterministic_attempt = connection.execute(
            """
            SELECT requested_model, attempted_model, provider, status, error_code
            FROM llm_attempts
            WHERE attempted_model = 'rules-v1'
            """
        ).fetchone()
        assert deterministic_attempt == (
            None,
            "rules-v1",
            "fallback",
            "skipped",
            "LEGACY_DETERMINISTIC_FALLBACK",
        )
        deterministic_analysis = connection.execute(
            """
            SELECT requested_model, effective_model, provider, llm_status
            FROM issue_analysis
            WHERE issue_id = (SELECT issue_id FROM issues WHERE issue_date = '2026-01-01')
            """
        ).fetchone()
        assert deterministic_analysis == (
            None,
            "rules-daily-analysis-v1",
            "fallback",
            "fallback",
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM legacy_publication_evidence").fetchone()[0]
            == 14
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM editorial_queue WHERE state='review'"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute(
            "SELECT target_issue_date, reason FROM editorial_queue WHERE state='deferred'"
        ).fetchone() == (None, "legacy daily deferred queue; last deferred for 2026-01-02")
        assert (
            connection.execute("SELECT COUNT(*) FROM content_releases WHERE sequence=0").fetchone()[
                0
            ]
            == 1
        )
        public_text = " ".join(
            str(value)
            for row in connection.execute("SELECT title, brief FROM issues")
            for value in row
        )
        assert "/root/" not in public_text
        assert "[local-path-removed]" in public_text
        with pytest.raises(BootstrapSealedError):
            legacy, manifest, manifest_sha, gazette = _create_synthetic_legacy(tmp_path / "repeat")
            import_legacy(
                legacy_db=legacy,
                target=connection,
                evidence_manifest=manifest,
                expected_manifest_sha256=manifest_sha,
                imported_at=IMPORTED_AT,
                gazette=GazetteInput(
                    gazette,
                    "gazettes/synthetic.html",
                    "2026-01",
                    "Synthetic",
                    "2026-01-03T00:00:00Z",
                ),
            )


def test_logical_hash_and_equivalence_ignore_file_layout(tmp_path: Path) -> None:
    target_path, _ = _import_synthetic(tmp_path)
    replica = tmp_path / "replica.sqlite"
    with sqlite3.connect(target_path) as source, sqlite3.connect(replica) as destination:
        source.backup(destination)
    equivalent, report = compare(target_path, replica)
    assert equivalent
    assert report["count_mismatches"] == {}
    assert report["table_hash_mismatches"] == {}
    with sqlite3.connect(target_path) as first, sqlite3.connect(replica) as second:
        assert database_digest(first) == database_digest(second)


def test_migrations_are_idempotent_and_checksum_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "migrations.sqlite"
    create_database(database, applied_at=IMPORTED_AT)
    with sqlite3.connect(database) as connection:
        assert apply_migrations(connection, applied_at="different-explicit-time") == ()
        connection.execute("UPDATE schema_migrations SET checksum = ?", ("0" * 64,))
        connection.commit()
        with pytest.raises(MigrationError, match="checksum mismatch"):
            apply_migrations(connection, applied_at=IMPORTED_AT)


def test_create_database_is_private_and_never_overwrites(tmp_path: Path) -> None:
    database = tmp_path / "private.sqlite"
    create_database(database, applied_at=IMPORTED_AT)
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    original = database.read_bytes()

    with pytest.raises(MigrationError, match="already exists"):
        create_database(database, applied_at=IMPORTED_AT)

    assert database.read_bytes() == original


def test_failed_import_rolls_back_without_bootstrap_seal(tmp_path: Path) -> None:
    legacy, manifest, _manifest_sha, _gazette = _create_synthetic_legacy(tmp_path)
    target_path = tmp_path / "target.sqlite"
    create_database(target_path, applied_at=IMPORTED_AT)
    with sqlite3.connect(target_path) as target, pytest.raises(Exception, match="manifest SHA-256"):
        import_legacy(
            legacy_db=legacy,
            target=target,
            evidence_manifest=manifest,
            expected_manifest_sha256="0" * 64,
            imported_at=IMPORTED_AT,
        )
    with sqlite3.connect(target_path) as target:
        assert target.execute("SELECT COUNT(*) FROM content_releases").fetchone()[0] == 0
        assert target.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
