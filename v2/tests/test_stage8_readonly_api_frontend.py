"""Stage 8 published-only API, pointer reload, frontend and static-route acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import jsonschema  # type: ignore[import-untyped]
import pytest
import yaml  # type: ignore[import-untyped]
from apps.api import (
    ActiveDatabaseManager,
    ApiResponse,
    DatabaseIdentity,
    RadarApi,
    RadarApplication,
    SearchRateLimiter,
)
from apps.api.__main__ import _application_release_id
from apps.api.database import PUBLIC_READ_OBJECTS
from apps.api.http_server import RadarHttpServer, remote_key
from apps.api.public_data import _card_search_text, _date_label, _shown_texts
from packages.domain.snapshot import JsonObject
from packages.publisher.local_simulation import install_initial_release, read_active_pointer
from packages.storage.hashing import logical_state_hash, verify_database
from packages.storage.migrations import create_database

ROOT = Path(__file__).resolve().parents[2]
V2_ROOT = ROOT / "v2"
OPENAPI = ROOT / "contracts/v1/public-api.openapi.yaml"
WEB_ROOT = V2_ROOT / "apps/web"
NOW = "2026-08-21T05:10:00Z"


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _build_release(path: Path, *, release_id: str, latest_title: str) -> None:
    create_database(path, applied_at="2026-08-21T00:00:00Z")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO application_compatibility VALUES (
              'app_stage8_synthetic', 1, '1.0.0', '1.0.0', '1.0.0',
              '1.0.0', '1.0.0', '1.0.0', '3.45.1', ?
            )
            """,
            (NOW,),
        )
        connection.execute("INSERT INTO rubrics VALUES ('orchestration', 'Оркестрация', 1)")
        connection.execute("INSERT INTO rubrics VALUES ('governance', 'Управление', 2)")
        connection.execute(
            """
            INSERT INTO materials VALUES (
              'material_public', 'Безопасная оркестрация агентов',
              'https://example.test/material', 'https://example.test/material',
              'Synthetic Journal', '2026-08-20T04:00:00Z', 'resolved',
              'Оркестрация стала надёжнее.', 'Проверять границы полномочий.',
              'Короткий проверенный сигнал.', ?, ?, ?
            )
            """,
            (_hash("material-public"), NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO issues VALUES (
              'issue_public_normal', '2026-08-20', 75, 'Обычный выпуск Radar',
              'Один опубликованный материал.', 'published', '2026-08-20T05:10:00Z',
              'v2', NULL, ?, ?, ?
            )
            """,
            (_hash("issue-public-normal"), NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO issue_materials VALUES (
              'issue_public_normal', 'material_public', 0, 'near', 'core',
              'Оркестрация стала надёжнее.', 'Проверять границы полномочий.',
              'Короткий проверенный сигнал.', '["Надёжность важнее скорости"]', NULL,
              '[]', 1, 95, 'strong', ?, ?
            )
            """,
            (NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO issue_analysis VALUES (
              'issue_public_normal', 'Контуры становятся надёжнее', ?, ?,
              'Проверенный редакционный вывод.', 'fallback', 'primary-model',
              'MiniMax-M3', 'minimax', 'stage8-synthetic-v1', ?
            )
            """,
            (
                _json(
                    {
                        "blocks": [
                            {
                                "kind": "signals",
                                "text": "Системы усиливают контроль границ.",
                                "title": "Главный сигнал",
                            }
                        ],
                        # Grounding metadata a V2-native analysis stores beside the
                        # blocks. It belongs to the record, not to the reader: the
                        # frozen Analysis schema is additionalProperties:false, so
                        # surfacing it fails the contract assertion below. Without a
                        # row that carries it, that assertion could never say so.
                        "evidenceMaterialIds": ["material_public"],
                        "inputContentHash": "0" * 64,
                    }
                ),
                _json([{"lead": "Контроль", "rest": "становится частью архитектуры."}]),
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO material_analysis VALUES (
              'issue_public_normal', 'material_public', 'Коротко от LLM', 'Угол AgPM от LLM',
              'fallback', 'primary-model', 'MiniMax-M3', 'minimax',
              'stage8-synthetic-v1', ?
            )
            """,
            (NOW,),
        )
        connection.execute(
            """
            INSERT INTO material_rubrics VALUES (
              'issue_public_normal', 'material_public', 'orchestration', 0.95, 'synthetic'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO material_quality VALUES (
              'issue_public_normal', 'material_public', 'resolved', 0,
              'ok', 'ok', NULL, ?
            )
            """,
            (NOW,),
        )
        connection.execute(
            """
            INSERT INTO daily_stats VALUES (
              'issue_public_normal', 3, 1, 2, 1, 0, 0, 1, 0, ?
            )
            """,
            (NOW,),
        )
        connection.execute(
            """
            INSERT INTO issues VALUES (
              'issue_public_empty', '2026-08-21', 76, ?, 'Пустой выпуск опубликован.',
              'published', ?, 'v2', 'no_qualifying_materials', ?, ?, ?
            )
            """,
            (latest_title, NOW, _hash(latest_title), NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO issue_analysis VALUES (
              'issue_public_empty', NULL, ?, '[]', 'Материалов нет.',
              'unavailable', 'primary-model', NULL, NULL, 'stage8-rules-v1', ?
            )
            """,
            (
                _json(
                    {
                        "blocks": [
                            {
                                "kind": "overview",
                                "text": "Квалифицирующих материалов нет.",
                                "title": "Итог",
                            }
                        ]
                    }
                ),
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO daily_stats VALUES (
              'issue_public_empty', 4, 0, 4, 0, 0, 0, 0, 0, ?
            )
            """,
            (NOW,),
        )
        connection.execute(
            """
            INSERT INTO issues VALUES (
              'issue_private_draft', '2099-01-01', NULL, 'PRIVATE DRAFT MUST NOT LEAK',
              '/mnt/private/draft', 'draft', NULL, NULL, NULL, ?, ?, ?
            )
            """,
            (_hash("private-draft"), NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO gazettes VALUES (
              'gazette_2026_08', '2026-08', 'Газета агентного управления',
              'published', ?, ?, ?, ?, ?
            )
            """,
            (NOW, _hash("gazette-manifest"), _hash("gazette-content"), NOW, NOW),
        )
        gazette_html = b"<!doctype html><title>Gazette</title><main>Published gazette</main>"
        connection.execute(
            """
            INSERT INTO gazette_assets VALUES (
              'gazette_2026_08', 'index.html', ?, ?, 'text/html'
            )
            """,
            (hashlib.sha256(gazette_html).hexdigest(), len(gazette_html)),
        )
        state_hash = logical_state_hash(connection)
        connection.execute(
            """
            INSERT INTO content_releases VALUES (
              ?, 1, NULL, ?, 'daily', 1, ?, ?, ?, ?
            )
            """,
            (release_id, f"candidate_{release_id}", "0" * 64, state_hash, NOW, NOW),
        )
        connection.commit()
        verify_database(connection)
        connection.commit()


def _refresh_release_state(connection: sqlite3.Connection) -> None:
    state_hash = logical_state_hash(connection)
    connection.execute(
        "UPDATE content_releases SET after_state_hash = ?",
        (state_hash,),
    )
    connection.commit()
    verify_database(connection)
    connection.commit()


@pytest.fixture
def stage8_runtime(
    tmp_path: Path,
) -> Iterator[tuple[ActiveDatabaseManager, RadarApi, Path]]:
    database = tmp_path / "stage8.sqlite"
    _build_release(database, release_id="release_stage8_a", latest_title="Пустой выпуск")
    active_root = tmp_path / "active"
    install_initial_release(active_root, database)
    manager = ActiveDatabaseManager(active_root)
    api = RadarApi(manager, application_release_id="app_release_stage8_fixture")
    yield manager, api, active_root
    manager.close()


def _payload(response: ApiResponse) -> object:
    return json.loads(response.body)


def _execute_sql(
    manager: ActiveDatabaseManager,
    statement: str,
) -> list[tuple[object, ...]]:
    def operation(
        connection: sqlite3.Connection,
        _identity: DatabaseIdentity,
    ) -> list[tuple[object, ...]]:
        return cast(list[tuple[object, ...]], connection.execute(statement).fetchall())

    return manager.execute(operation)


def _rewrite_refs(value: object) -> object:
    if isinstance(value, list):
        return [_rewrite_refs(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                raw.replace("#/components/schemas/", "#/$defs/")
                if key == "$ref" and isinstance(raw, str)
                else _rewrite_refs(raw)
            )
            for key, raw in value.items()
        }
    return value


def _schema_validator(name: str) -> jsonschema.Draft202012Validator:
    openapi = cast(dict[str, object], yaml.safe_load(OPENAPI.read_text(encoding="utf-8")))
    components = cast(dict[str, object], openapi["components"])
    schemas = cast(dict[str, object], components["schemas"])
    schema = {
        "$defs": _rewrite_refs(schemas),
        "$ref": f"#/$defs/{name}",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
    }
    return jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )


def _assert_schema(name: str, value: object) -> None:
    errors = list(_schema_validator(name).iter_errors(value))
    assert not errors, [error.message for error in errors]


def test_all_openapi_endpoints_are_published_only_and_schema_valid(
    stage8_runtime: tuple[ActiveDatabaseManager, RadarApi, Path],
) -> None:
    _manager, api, active_root = stage8_runtime
    cases = (
        ("/api/health", "Health"),
        ("/api/latest", "IssueDetail"),
        ("/api/issues/2026-08-20", "IssueDetail"),
        ("/api/stats?period=30d", "Stats"),
    )
    bodies: list[object] = []
    for target, schema in cases:
        response = api.handle("GET", target, request_id="stage8_contract")
        assert response.status == 200
        payload = _payload(response)
        _assert_schema(schema, payload)
        bodies.append(payload)

    issue_list = cast(dict[str, object], _payload(api.handle("GET", "/api/issues?limit=1")))
    assert set(issue_list) == {"items", "nextCursor"}
    for item in cast(list[object], issue_list["items"]):
        _assert_schema("IssueSummary", item)
    assert issue_list["nextCursor"] is not None
    second_page = cast(
        dict[str, object],
        _payload(api.handle("GET", f"/api/issues?limit=1&cursor={issue_list['nextCursor']}")),
    )
    assert len(cast(list[object], second_page["items"])) == 1

    for target in (
        "/api/materials?period=30d&limit=100",
        "/api/search?q=%D0%B0%D0%B3%D0%B5%D0%BD%D1%82%D0%BE%D0%B2&period=30d&limit=100",
    ):
        payload = cast(dict[str, object], _payload(api.handle("GET", target)))
        for item in cast(list[object], payload["items"]):
            _assert_schema("Material", item)
        assert len(cast(list[object], payload["items"])) == 1
        bodies.append(payload)

    timeseries = cast(dict[str, object], _payload(api.handle("GET", "/api/timeseries?days=7")))
    for item in cast(list[object], timeseries["items"]):
        _assert_schema("TimeseriesPoint", item)
    rubrics = cast(list[object], _payload(api.handle("GET", "/api/rubrics?period=30d")))
    sources = cast(list[object], _payload(api.handle("GET", "/api/sources?period=30d")))
    for item in rubrics:
        _assert_schema("Rubric", item)
    for item in sources:
        _assert_schema("Source", item)
    assert rubrics == [
        {
            "anchorDate": "2026-08-21",
            "confidence": "low",
            "count": 1,
            "currentCount": 1,
            "currentShare": 1.0,
            "currentTotal": 1,
            "direction": "flat",
            "id": "orchestration",
            "index": 0.0,
            "period": "30d",
            "previousCount": 0,
            "previousShare": 1.0,
            "previousTotal": 0,
            "title": "Оркестрация",
        }
    ]
    assert sources == [{"included": 1, "name": "Synthetic Journal"}]
    last_material_page = cast(dict[str, object], bodies[-1])
    material = cast(list[dict[str, object]], last_material_page["items"])[0]
    assert material["llmShortText"] == "Коротко от LLM"
    assert material["llmAgpmAngle"] == "Угол AgPM от LLM"

    gazettes = cast(dict[str, object], _payload(api.handle("GET", "/api/gazettes")))
    for item in cast(list[object], gazettes["items"]):
        _assert_schema("Gazette", item)
    # The address of the issue, not of the directory it lives in: a revision gets
    # a new asset path, because the route is served immutable for a year.
    assert (
        cast(list[dict[str, object]], gazettes["items"])[0]["url"] == "/gazettes/2026-08/index.html"
    )
    bodies.extend((timeseries, rubrics, sources, gazettes))

    serialized = json.dumps(bodies, ensure_ascii=False)
    assert "PRIVATE DRAFT MUST NOT LEAK" not in serialized
    assert str(active_root) not in serialized
    assert "/mnt/" not in serialized
    assert "issue_private_draft" not in serialized


def test_empty_no_llm_and_direct_internal_sql_are_explicit(
    stage8_runtime: tuple[ActiveDatabaseManager, RadarApi, Path],
) -> None:
    manager, api, _root = stage8_runtime
    latest = cast(dict[str, object], _payload(api.handle("GET", "/api/latest")))
    assert latest["materialCount"] == 0
    assert latest["materials"] == []
    assert latest["llm"] == {"effectiveModel": None, "status": "unavailable"}
    assert cast(dict[str, int], latest["stats"])["viewed"] == 4
    for statement in (
        "SELECT title FROM issues",
        "PRAGMA user_version",
        "SELECT load_extension('untrusted')",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
            _execute_sql(manager, statement)


@pytest.mark.parametrize(
    "target",
    (
        "/api/issues?limit=0",
        "/api/issues?limit=01",
        "/api/issues?limit=1&limit=2",
        "/api/issues?unknown=1",
        "/api/issues/%32%30%32%36-08-20",
        "/api/search?q=",
        "/api/search?q=%00bad",
        "/api/stats",
        "/api/timeseries?days=91",
        "/api/materials?perimeter=inside",
        "/api/rubrics?anchor=2026-08-32",
        # A date past the edge of the archive is a caller error, not a database
        # outage: this used to answer 503 "Published data is unavailable".
        "/api/rubrics?anchor=2099-01-01",
    ),
)
def test_malformed_inputs_are_bounded_json_errors(
    stage8_runtime: tuple[ActiveDatabaseManager, RadarApi, Path],
    target: str,
) -> None:
    _manager, api, _root = stage8_runtime
    response = api.handle("GET", target, request_id="bounded_case")
    assert response.status == 400
    payload = _payload(response)
    _assert_schema("Error", payload)
    assert payload == {
        "code": "INVALID_REQUEST",
        "message": "The request is invalid",
        "requestId": "bounded_case",
    }


def test_unknown_write_and_rate_limit_fail_closed(
    stage8_runtime: tuple[ActiveDatabaseManager, RadarApi, Path],
) -> None:
    manager, _api, _root = stage8_runtime
    api = RadarApi(
        manager,
        application_release_id="app_release_rate_limit_test",
        search_limiter=SearchRateLimiter(requests=1, window_seconds=60),
    )
    assert api.handle("POST", "/api/issues").status == 405
    assert api.handle("PUT", "/api/issues").status == 405
    assert api.handle("DELETE", "/api/issues").status == 405
    assert api.handle("GET", "/api/internal/date-quality").status == 404
    assert api.handle("GET", "/api/search?q=agent", remote_key="test").status == 200
    limited = api.handle("GET", "/api/search?q=agent", remote_key="test")
    assert limited.status == 429
    _assert_schema("Error", _payload(limited))


def test_path_shaped_published_values_fail_closed_without_leaking(tmp_path: Path) -> None:
    database = tmp_path / "unsafe-public.sqlite"
    _build_release(database, release_id="release_stage8_unsafe", latest_title="Пустой выпуск")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE materials SET source_name = ? WHERE material_id = 'material_public'",
            ("/mnt/private/source",),
        )
        connection.execute(
            "UPDATE gazettes SET title = ? WHERE gazette_id = 'gazette_2026_08'",
            ("/root/private/gazette",),
        )
        _refresh_release_state(connection)
    active_root = tmp_path / "unsafe-active"
    install_initial_release(active_root, database)
    manager = ActiveDatabaseManager(active_root)
    api = RadarApi(manager, application_release_id="app_release_http_test")
    try:
        for target in ("/api/issues", "/api/sources", "/api/gazettes"):
            response = api.handle("GET", target, request_id="unsafe_public")
            assert response.status == 503
            decoded = response.body.decode("utf-8")
            assert "private/source" not in decoded
            assert "private/gazette" not in decoded
            _assert_schema("Error", _payload(response))
    finally:
        manager.close()


@pytest.mark.parametrize(
    ("legacy_inferred", "date_status", "severity", "expected_status"),
    (
        (True, "low_confidence", "medium", 200),
        (True, "resolved", "high", 200),
        (False, "low_confidence", "medium", 503),
    ),
)
def test_only_queued_legacy_quality_anomaly_can_exceed_issue_window(
    tmp_path: Path,
    legacy_inferred: bool,
    date_status: str,
    severity: str,
    expected_status: int,
) -> None:
    suffix = f"{'legacy' if legacy_inferred else 'native'}-{date_status}"
    database = tmp_path / f"date-window-{suffix}.sqlite"
    _build_release(
        database,
        release_id=f"release_stage8_date_{suffix}",
        latest_title="Пустой выпуск",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE materials
            SET published_at = '2026-09-08', publication_date_status = ?
            WHERE material_id = 'material_public'
            """,
            (date_status,),
        )
        connection.execute(
            """
            UPDATE material_quality
            SET publication_date_status = ?, issue_date_delta_days = 19,
                severity = ?, review_status = 'queued', reason = 'Known Legacy date anomaly'
            WHERE issue_id = 'issue_public_normal' AND material_id = 'material_public'
            """,
            (date_status, severity),
        )
        if legacy_inferred:
            connection.execute(
                """
                UPDATE issues SET publication_origin = 'legacy_inferred'
                WHERE issue_id = 'issue_public_normal'
                """
            )
            connection.execute(
                """
                INSERT INTO legacy_issue_provenance VALUES (
                  'issue_public_normal', 'published', '2026-08-20', ?, ?, ?
                )
                """,
                (_hash("legacy-baseline"), _hash("legacy-row"), NOW),
            )
        _refresh_release_state(connection)
    active_root = tmp_path / f"date-active-{suffix}"
    install_initial_release(active_root, database)
    manager = ActiveDatabaseManager(active_root)
    try:
        response = RadarApi(manager, application_release_id="app_release_date_status_test").handle(
            "GET", "/api/materials?period=30d&limit=100"
        )
        assert response.status == expected_status
        if legacy_inferred:
            payload = cast(dict[str, object], _payload(response))
            item = cast(list[dict[str, object]], payload["items"])[0]
            assert item["publicationDateStatus"] == date_status
            assert item["publishedAt"] == "2026-09-08T00:00:00Z"
    finally:
        manager.close()


def test_atomic_pointer_switch_reopens_expected_release_and_inode(
    stage8_runtime: tuple[ActiveDatabaseManager, RadarApi, Path],
    tmp_path: Path,
) -> None:
    _manager, api, active_root = stage8_runtime
    first = cast(dict[str, object], _payload(api.handle("GET", "/api/health")))
    assert first["releaseId"] == "release_stage8_a"
    # Built once and kept: the same document object answers the next request.
    before = cast(dict[str, object], _payload(api.handle("GET", "/api/latest")))
    cached_hash, cached = api._issue_cache
    assert cached_hash == first["databaseStateHash"]
    assert cached[cast(str, before["issueDate"])] is not None
    api.handle("GET", "/api/latest")
    assert api._issue_cache[1] is cached

    replacement_database = tmp_path / "replacement.sqlite"
    _build_release(
        replacement_database,
        release_id="release_stage8_b",
        latest_title="Пустой выпуск после переключения",
    )
    replacement_root = tmp_path / "replacement-active"
    replacement_pointer = install_initial_release(replacement_root, replacement_database)
    target_database = active_root / replacement_pointer.database
    shutil.copyfile(replacement_pointer.database_path, target_database)
    target_database.chmod(0o600)
    pointer_next = active_root / ".active.stage8.next"
    pointer_next.write_bytes((replacement_root / "active.json").read_bytes())
    pointer_next.chmod(0o600)
    os.replace(pointer_next, active_root / "active.json")

    second = cast(dict[str, object], _payload(api.handle("GET", "/api/health")))
    assert second["releaseId"] == "release_stage8_b"
    assert second["databaseStateHash"] != first["databaseStateHash"]
    latest = cast(dict[str, object], _payload(api.handle("GET", "/api/latest")))
    assert latest["title"] == "Пустой выпуск после переключения"
    assert read_active_pointer(active_root).release_id == "release_stage8_b"
    # The documents of release A went with it: the cache is keyed by the new hash.
    assert api._issue_cache[0] == second["databaseStateHash"]
    assert api._issue_cache[1] is not cached


def test_health_exposes_explicit_runtime_application_release(
    stage8_runtime: tuple[ActiveDatabaseManager, RadarApi, Path],
) -> None:
    manager, _api, _active_root = stage8_runtime
    payload = cast(
        dict[str, object],
        _payload(
            RadarApi(manager, application_release_id="app_release_stage8_runtime").handle(
                "GET", "/api/health"
            )
        ),
    )
    assert payload["applicationReleaseId"] == "app_release_stage8_runtime"


def test_invalid_runtime_application_release_is_rejected(
    stage8_runtime: tuple[ActiveDatabaseManager, RadarApi, Path],
) -> None:
    manager, _api, _active_root = stage8_runtime
    with pytest.raises(ValueError, match="application release id"):
        RadarApi(manager, application_release_id="../unsafe")


def test_runtime_application_release_marker_is_immutable_and_required(tmp_path: Path) -> None:
    marker = tmp_path / "APPLICATION-RELEASE.json"
    marker.write_text('{"applicationReleaseId":"app_release_marker_test"}\n', encoding="utf-8")
    marker.chmod(0o400)
    assert _application_release_id(tmp_path) == "app_release_marker_test"


def test_spa_assets_gazette_and_missing_routes_are_separate(
    stage8_runtime: tuple[ActiveDatabaseManager, RadarApi, Path],
    tmp_path: Path,
) -> None:
    _manager, api, _root = stage8_runtime
    gazette_root = tmp_path / "gazettes"
    period_root = gazette_root / "2026-08"
    period_root.mkdir(parents=True, mode=0o700)
    (period_root / "index.html").write_text(
        "<!doctype html><title>Gazette</title><main>Published gazette</main>",
        encoding="utf-8",
    )
    (period_root / "index.html").chmod(0o600)
    application = RadarApplication(api, web_root=WEB_ROOT, gazette_root=gazette_root)

    spa = application.handle("GET", "/issues/2026-08-20")
    assert spa.status == 200
    assert b'<main id="top"' in spa.body
    assert dict(spa.headers)["Cache-Control"] == "no-store"
    asset = application.handle("GET", "/assets/app.mjs?v=stage8-v1")
    assert asset.status == 200
    assert dict(asset.headers)["Cache-Control"].endswith("immutable")
    font = application.handle("GET", "/assets/fonts/PTMono-Regular.ttf")
    assert font.status == 200
    assert dict(font.headers)["Content-Type"] == "font/ttf"
    # Issues are published content now, not files inside the application.
    assert application.handle("GET", "/gazette-20260803.html").status == 404
    assert application.handle("GET", "/gazette-20260901-r3.html").status == 404
    gazette = application.handle("GET", "/gazettes/2026-08/")
    assert gazette.status == 200
    assert b"Published gazette" in gazette.body
    # The address an already-sent notification carries: the frontend reads it
    # and replaces it with /issues/<date>, and the service must not 404 it.
    legacy_link = application.handle("GET", "/?date=2026-08-20")
    assert legacy_link.status == 200
    assert b'<main id="top"' in legacy_link.body
    assert application.handle("GET", "/?date=20260820").status == 404
    assert application.handle("GET", "/?date=2026-08-20&extra=1").status == 404
    assert application.handle("GET", "/issues/2026-08-20?date=2026-08-20").status == 404
    assert "script-src 'none'" in dict(gazette.headers)["Content-Security-Policy"]

    (period_root / "unlisted.txt").write_text("must not be served", encoding="utf-8")
    (period_root / "unlisted.txt").chmod(0o600)
    assert application.handle("GET", "/gazettes/2026-08/unlisted.txt").status == 404
    (period_root / "index.html").write_text("tampered", encoding="utf-8")
    assert application.handle("GET", "/gazettes/2026-08/").status == 404

    assert application.handle("GET", "/assets/missing.js").status == 404
    assert application.handle("GET", "/gazettes/2026-07/").status == 404
    assert application.handle("GET", "/gazettes/2026-08/missing.png").status == 404
    assert application.handle("GET", "/gazettes/2026-08/../index.html").status == 404
    assert application.handle("GET", "/gazettes/2026-08/%2e%2e/index.html").status == 404
    assert application.handle("GET", "/unknown-real-file.js").status == 404


def test_loopback_http_transport_serves_json_and_security_headers(
    stage8_runtime: tuple[ActiveDatabaseManager, RadarApi, Path],
    tmp_path: Path,
) -> None:
    _manager, api, _root = stage8_runtime
    gazette_root = tmp_path / "http-gazettes"
    gazette_root.mkdir(mode=0o700)
    application = RadarApplication(api, web_root=WEB_ROOT, gazette_root=gazette_root)
    with RadarHttpServer(("127.0.0.1", 0), application) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health",
                timeout=5,
            ) as response:
                assert response.status == 200
                assert response.headers["Content-Type"] == "application/json; charset=utf-8"
                assert response.headers["X-Content-Type-Options"] == "nosniff"
                _assert_schema("Health", json.loads(response.read()))
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/issues",
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as captured:
                urllib.request.urlopen(request, timeout=5)  # noqa: S310 -- fixed loopback endpoint
            assert captured.value.code == 405
            assert captured.value.headers["Content-Type"] == "application/json; charset=utf-8"
            _assert_schema("Error", json.loads(captured.value.read()))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_a_freshly_written_asset_is_served_on_its_first_read(tmp_path: Path) -> None:
    """On a relatime mount the first read after a write moves atime.

    The stat comparison around the read included it, so every freshly deployed
    asset answered 404 once and 200 from then on. Seen 2026-09-05: the gate went
    red for one run after `index.html` changed, and green when run again.
    """
    from apps.api.application import _read_static

    web_root = tmp_path / "web"
    web_root.mkdir(mode=0o700)
    asset = web_root / "app.mjs"
    asset.write_text("export const fresh = true;\n", encoding="utf-8")
    asset.chmod(0o644)
    written = asset.stat()
    # A read older than the write is what relatime updates on the next read.
    os.utime(asset, ns=(written.st_mtime_ns - 10**11, written.st_mtime_ns))

    assert _read_static(web_root, "app.mjs") == b"export const fresh = true;\n"
    assert _read_static(web_root, "app.mjs") == b"export const fresh = true;\n"


def test_search_allowance_is_per_reader_behind_the_proxy_and_head_is_answered(
    stage8_runtime: tuple[ActiveDatabaseManager, RadarApi, Path],
    tmp_path: Path,
) -> None:
    """Every request reaches the process from 127.0.0.1: Caddy is the only client.

    Keyed on the socket address, the search window was one window for the whole
    site, and thirty searches by anyone closed search for everyone for a minute.
    Caddy sets X-Forwarded-For itself, so that is the reader.
    """
    manager, _api, _root = stage8_runtime
    api = RadarApi(
        manager,
        application_release_id="app_release_forwarded_test",
        search_limiter=SearchRateLimiter(requests=1, window_seconds=60),
    )
    gazette_root = tmp_path / "forwarded-gazettes"
    gazette_root.mkdir(mode=0o700)
    application = RadarApplication(api, web_root=WEB_ROOT, gazette_root=gazette_root)
    with RadarHttpServer(("127.0.0.1", 0), application) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])

        def search(reader: str | None) -> int:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/search?q=agent")
            if reader is not None:
                request.add_header("X-Forwarded-For", reader)
            try:
                with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                    return int(response.status)
            except urllib.error.HTTPError as error:
                return int(error.code)

        try:
            assert search("203.0.113.5") == 200
            assert search("203.0.113.6") == 200
            assert search("203.0.113.5") == 429
            # Without the header - a loopback smoke - the socket address is the key.
            assert search(None) == 200
            assert search(None) == 429

            head = urllib.request.Request(f"http://127.0.0.1:{port}/api/health", method="HEAD")
            with urllib.request.urlopen(head, timeout=5) as response:  # noqa: S310
                assert response.status == 200
                assert response.read() == b""
                head_length = int(response.headers["Content-Length"])
                assert response.headers["Server"] == "radar-v2"
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=5
            ) as response:
                assert len(response.read()) == head_length
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
    assert remote_key("198.51.100.7, 127.0.0.1", "127.0.0.1") == "198.51.100.7"
    assert remote_key(None, "127.0.0.1") == "127.0.0.1"


def test_frontend_has_mobile_empty_no_llm_and_dom_only_security_contract() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    script = (WEB_ROOT / "app.mjs").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
    assert 'name="viewport"' in html
    assert '<link rel="icon" href="/favicon.svg?v=20260729-1734"' in html
    assert '<meta property="og:url" content="https://radar.agpm.space/">' in html
    assert (
        '<meta property="og:image" content="https://radar.agpm.space/og-image-20260803.png">'
    ) in html
    assert '<meta property="og:image:width" content="1200">' in html
    assert '<meta property="og:image:height" content="630">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html
    assert hashlib.sha256((WEB_ROOT / "favicon.svg").read_bytes()).hexdigest() == (
        "4757342b86258c1fd7f9e08c4bc66b5e6af3014d5c6ab4b8ca1a4914524e7b38"
    )
    assert hashlib.sha256((WEB_ROOT / "og-image-20260803.png").read_bytes()).hexdigest() == (
        "1805d2711f4f7a4dd6118afc9900a314472383ace8ad9c0c98c26281f0c2b430"
    )
    assert "/assets/app.mjs?v=" in html and "/assets/styles.css?v=" in html
    assert "document.write" not in script
    assert "safeExternalUrl" in script
    assert "escapeHtml" in script
    assert "legacyIssue" in script
    assert "/api/latest" in script
    assert "getJson(`/api/stats?period=${period}`)" in script
    assert "let reloadGeneration = 0" in script
    # Actual stale-response behaviour is exercised by both console race smokes;
    # the counter may also advance when a query is invalidated before debounce.
    assert "frontend_recovery_smoke.mjs" in (V2_ROOT / "scripts/verify.sh").read_text()
    assert "if (generation !== reloadGeneration) return" in script
    assert "loadIssueMaterials(request)" in script
    assert "loadPeriodStats(request.period)" in script
    assert "page.nextCursor || null" in script
    assert "while (cursor)" in script
    assert 'short_text: llm.status === "success" ? (item.llmShortText || "") : ""' in script
    assert (
        'const llmSucceeded = item.llm_summary && item.llm_summary.status === "success"' in script
    )
    assert 'description: (llmSucceeded ? item.llm_summary.short_text : "")' in script
    assert 'takeaway: (llmSucceeded ? item.llm_summary.agpm_angle : "")' in script
    # Rendering and search take the card texts from the same rule.
    assert script.count("= cardView(item)") == 2
    assert 'signal: block("overview")' in script
    assert 'why_agpm: block("signals")' in script
    assert "Сонар" in html
    assert "Динамика трендов" in html
    assert "Хронология выпусков" not in html
    assert "Источники выпуска</h2>" not in html
    assert "ИСТОЧНИКИ ВЫПУСКА" in html
    assert "Рубрикатор" in html
    # The archive and the frame come from /api/gazettes now: no issue is named in
    # the markup, and the module has to be the only place that knows the address.
    assert re.search(r"gazette-\d{8}", html) is None
    assert 'id="gazetteArchiveRows"' in html
    assert "loadGazettes" in script
    assert "@media (max-width: 1100px)" in styles
    assert "@media (max-width: 760px)" in styles
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(  # noqa: S603 -- absolute trusted development executable
        [node, "--check", str(WEB_ROOT / "app.mjs")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_active_release_file_remains_byte_stable_after_public_queries(
    stage8_runtime: tuple[ActiveDatabaseManager, RadarApi, Path],
) -> None:
    _manager, api, active_root = stage8_runtime
    pointer = read_active_pointer(active_root)
    before = hashlib.sha256(pointer.database_path.read_bytes()).hexdigest()
    for target in (
        "/api/health",
        "/api/latest",
        "/api/issues",
        "/api/materials",
        "/api/search?q=agent",
        "/api/stats?period=30d",
        "/api/timeseries",
        "/api/rubrics",
        "/api/sources",
        "/api/gazettes",
    ):
        assert api.handle("GET", target).status == 200
    after = hashlib.sha256(pointer.database_path.read_bytes()).hexdigest()
    assert after == before
    assert not Path(f"{pointer.database_path}-wal").exists()
    assert not Path(f"{pointer.database_path}-shm").exists()
    assert not Path(f"{pointer.database_path}-journal").exists()


def test_the_contract_and_the_authorizer_name_the_same_objects() -> None:
    """Two texts of one rule, checked against each other.

    `apiReadAllowlist` in the contract and `PUBLIC_READ_OBJECTS` in
    apps/api/database.py both say what a public query may read, and the second
    is what the SQLite authorizer actually enforces. Nothing compared them:
    dropping a name from one and not the other would have gone unnoticed until
    a reader hit either a 503 or an object the contract never allowed.
    """
    contract = cast(
        dict[str, object],
        yaml.safe_load(OPENAPI.parent.joinpath("sqlite-contract.yaml").read_text(encoding="utf-8")),
    )
    assert set(cast(list[str], contract["apiReadAllowlist"])) == PUBLIC_READ_OBJECTS


def test_search_looks_only_at_the_texts_a_card_shows(
    stage8_runtime: tuple[ActiveDatabaseManager, RadarApi, Path],
) -> None:
    _manager, api, _active_root = stage8_runtime
    # The fixture material's analysis is a fallback: the card shows the brief and the
    # rule-based takeaway, never the stored model texts and never the longer summary.
    cases = (
        ("Короткий проверенный", 1),  # brief, shown
        ("границы полномочий", 1),  # rule-based takeaway, shown
        ("агентов", 1),  # title
        ("стала надёжнее", 0),  # summary, hidden behind the brief
        ("Коротко от LLM", 0),  # model text of a fallback analysis, not shown
        ("example.test", 1),  # the host the card prints for the source
        ("Synthetic Journal", 0),  # the source name is not on the card when a host is
        ("Сильный сигнал", 1),  # the signal label of the card
        ("опубл. 20 авг", 1),  # the date line of the card
        ("Оркестрация", 1),  # the rubric tag
    )
    for query, expected in cases:
        target = "/api/search?period=30d&" + urllib.parse.urlencode({"q": query})
        payload = cast(dict[str, object], _payload(api.handle("GET", target)))
        assert len(cast(list[object], payload["items"])) == expected, query


def test_shown_texts_follow_the_card_rule() -> None:
    succeeded: JsonObject = {
        "llm": {"status": "success"},
        "llmShortText": "Факты модели",
        "llmAgpmAngle": "Вывод модели",
        "brief": "краткий шаблон",
        "summary": "шаблон",
        "agpmTakeaway": "шаблонный вывод",
    }
    assert _shown_texts(succeeded) == ("Факты модели", "Вывод модели")
    fallback: JsonObject = {**succeeded, "llm": {"status": "fallback"}}
    assert _shown_texts(fallback) == ("краткий шаблон", "шаблонный вывод")
    without_brief: JsonObject = {**fallback, "brief": None}
    assert _shown_texts(without_brief) == ("шаблон", "шаблонный вывод")
    assert _shown_texts({"llm": {"status": "success"}, "summary": "только шаблон"}) == (
        "только шаблон",
        "",
    )


def test_card_search_text_is_everything_the_card_shows() -> None:
    item: JsonObject = {
        "llm": {"status": "success"},
        "llmShortText": "Klarna передала агенту чаты",
        "llmAgpmAngle": "Выбирать сценарий по baseline",
        "brief": "скрытый шаблон",
        "summary": "скрытый шаблон подлиннее",
        "agpmTakeaway": "скрытый вывод",
        "title": "Заголовок",
        "url": "https://www.monday.com/blog/ai-workspace/",
        "sourceName": "Brave web research: near",
        "publishedAt": "2026-09-01T04:00:00Z",
        "issueDate": "2026-09-02",
        "signalStrength": "context",
        "verdict": "core",
        "rubrics": ["governance", "orchestration", "isup", "fourth"],
    }
    text = _card_search_text(item, {"governance": "Управление", "orchestration": "Оркестрация"})
    for shown in (
        "контекст",
        "monday.com",
        "опубл. 1 сент",
        "заголовок",
        "klarna",
        "baseline",
        "управление",
        "оркестрация",
        "isup",
    ):
        assert shown in text, shown
    for hidden in ("www.", "brave", "скрытый", "fourth"):
        assert hidden not in text, hidden
    assert _date_label({"issueDate": "2026-09-02"}) == "дата публикации не найдена · выпуск 2 сент"
    assert _date_label({}) == "дата публикации не найдена"
