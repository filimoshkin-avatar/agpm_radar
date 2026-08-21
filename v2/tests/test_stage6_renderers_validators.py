"""Stage 6 deterministic renderer, invariant, gazette and golden regressions."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import sqlite3
import zipfile
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import cast

import jsonschema  # type: ignore[import-untyped]
import pytest
import yaml  # type: ignore[import-untyped]
from packages.domain.snapshot import JsonObject
from packages.renderers.daily_docx import (
    render_daily_docx,
    render_public_issue_docx,
)
from packages.renderers.daily_json import render_daily_json
from packages.storage.hashing import logical_state_hash
from packages.storage.migrations import create_database
from packages.validation.artifacts import (
    ArtifactValidationError,
    validate_daily_docx,
    validate_daily_json,
)
from packages.validation.gazette import (
    GazetteValidationError,
    validate_gazette_candidate,
)
from packages.validation.public_issue import (
    PublicIssueValidationError,
    build_public_issue,
    validate_public_issue_document,
)

ROOT = Path(__file__).resolve().parents[2]
V2_ROOT = ROOT / "v2"
GOLDEN_PATH = V2_ROOT / "fixtures/synthetic/stage6-golden.json"
OPENAPI_PATH = ROOT / "contracts/v1/public-api.openapi.yaml"
LEGACY_FIXTURE = ROOT / "fixtures/legacy-baseline/deterministic-fallback-2026-08-15.json"
NOW = "2026-08-20T05:10:00Z"


def _golden() -> dict[str, object]:
    return cast(dict[str, object], json.loads(GOLDEN_PATH.read_text(encoding="utf-8")))


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _document(case: str) -> JsonObject:
    selected = cast(dict[str, object], _golden()[case])
    return cast(JsonObject, selected["document"])


def _seed_database(
    path: Path,
    document: Mapping[str, object],
    *,
    raw_analysis_blocks: list[object] | None = None,
) -> None:
    create_database(path, applied_at=NOW)
    issue_date = cast(str, document["issueDate"])
    issue_id = "issue_" + issue_date.replace("-", "")
    created_at = cast(str, document["publishedAt"] or f"{issue_date}T00:00:00Z")
    stats = cast(dict[str, int], document["stats"])
    analysis = cast(dict[str, object], document["analysis"])
    issue_llm = cast(dict[str, object], document["llm"])
    materials = cast(list[dict[str, object]], document["materials"])
    rubric_ids = sorted(
        {
            cast(str, rubric)
            for material in materials
            for rubric in cast(list[object], material["rubrics"])
        }
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO application_compatibility VALUES (
              'app_stage6_synthetic', 1, '1.0.0', '1.0.0', '1.0.0',
              '1.0.0', '1.0.0', '1.0.0', '3.45.1', ?
            )
            """,
            (created_at,),
        )
        for index, rubric_id in enumerate(rubric_ids):
            connection.execute(
                "INSERT INTO rubrics(rubric_id, title, sort_order) VALUES (?, ?, ?)",
                (rubric_id, rubric_id.title(), index),
            )
        connection.execute(
            """
            INSERT INTO issues VALUES (?, ?, ?, ?, ?, 'published', ?, 'v2', NULL, ?, ?, ?)
            """,
            (
                issue_id,
                issue_date,
                document["issueNumber"],
                document["title"],
                document["brief"],
                document["publishedAt"],
                _digest(document),
                created_at,
                created_at,
            ),
        )
        blocks = analysis["blocks"] if raw_analysis_blocks is None else raw_analysis_blocks
        connection.execute(
            """
            INSERT INTO issue_analysis VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'synthetic',
                                                'stage6-synthetic-v1', ?)
            """,
            (
                issue_id,
                analysis["headline"],
                json.dumps(
                    {"blocks": blocks}, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ),
                json.dumps(
                    document["theses"], ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ),
                analysis["brief"],
                issue_llm["status"],
                issue_llm["effectiveModel"],
                created_at,
            ),
        )
        for position, material in enumerate(materials):
            material_id = cast(str, material["id"])
            connection.execute(
                """
                INSERT INTO materials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    material_id,
                    material["title"],
                    material["url"],
                    material["canonicalUrl"],
                    material["sourceName"],
                    material["publishedAt"],
                    material["publicationDateStatus"],
                    material["summary"],
                    material["agpmTakeaway"],
                    material["brief"],
                    _digest(material),
                    created_at,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO issue_materials VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?
                )
                """,
                (
                    issue_id,
                    material_id,
                    position,
                    material["perimeter"],
                    material["verdict"],
                    material["summary"],
                    material["agpmTakeaway"],
                    material["brief"],
                    json.dumps(material["theses"], ensure_ascii=False, separators=(",", ":")),
                    material["trendNotes"],
                    1 if material["keyMaterial"] else 0,
                    material["signalScore"],
                    material["signalStrength"],
                    created_at,
                    created_at,
                ),
            )
            material_llm = cast(dict[str, object], material["llm"])
            connection.execute(
                """
                INSERT INTO material_analysis VALUES (?, ?, NULL, NULL, ?, NULL, ?,
                                                      'synthetic', 'stage6-synthetic-v1', ?)
                """,
                (
                    issue_id,
                    material_id,
                    material_llm["status"],
                    material_llm["effectiveModel"],
                    created_at,
                ),
            )
            published_at = cast(str | None, material["publishedAt"])
            delta = None
            if published_at is not None:
                delta = (
                    datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").date()
                    - date.fromisoformat(issue_date)
                ).days
            date_status = cast(str, material["publicationDateStatus"])
            severity, review, reason = (
                ("ok", "ok", None)
                if date_status == "resolved"
                else ("low", "monitor", "Synthetic low confidence")
                if date_status == "low_confidence"
                else ("medium", "queued", "Synthetic unresolved date")
            )
            connection.execute(
                """
                INSERT INTO material_quality VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issue_id,
                    material_id,
                    date_status,
                    delta,
                    severity,
                    review,
                    reason,
                    created_at,
                ),
            )
            for rubric_id in cast(list[str], material["rubrics"]):
                connection.execute(
                    "INSERT INTO material_rubrics VALUES (?, ?, ?, NULL, 'synthetic')",
                    (issue_id, material_id, rubric_id),
                )
        connection.execute(
            """
            INSERT INTO daily_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue_id,
                stats["viewed"],
                stats["included"],
                stats["cut"],
                stats["near"],
                stats["mid"],
                stats["far"],
                stats["core"],
                stats["adjacent"],
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO issues VALUES (
              'issue_synthetic_draft', '2099-01-01', NULL, 'PRIVATE DRAFT MUST NOT LEAK',
              NULL, 'draft', NULL, NULL, NULL, ?, ?, ?
            )
            """,
            ("f" * 64, created_at, created_at),
        )
        state_hash = logical_state_hash(connection)
        connection.execute(
            """
            INSERT INTO content_releases VALUES (
              'release_stage6_synthetic', 0, NULL, 'candidate_stage6_synthetic', 'daily', 1,
              ?, ?, ?, ?
            )
            """,
            ("0" * 64, state_hash, created_at, created_at),
        )
        connection.commit()


def _rewrite_openapi_refs(value: object) -> object:
    if isinstance(value, list):
        return [_rewrite_openapi_refs(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                raw.replace("#/components/schemas/", "#/$defs/")
                if key == "$ref" and isinstance(raw, str)
                else _rewrite_openapi_refs(raw)
            )
            for key, raw in value.items()
        }
    return value


def _assert_openapi_issue(document: Mapping[str, object]) -> None:
    openapi = cast(dict[str, object], yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8")))
    components = cast(dict[str, object], openapi["components"])
    schemas = cast(dict[str, object], components["schemas"])
    schema = {
        "$defs": _rewrite_openapi_refs(schemas),
        "$ref": "#/$defs/IssueDetail",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
    }
    jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    ).validate(document)


def test_normal_json_docx_are_byte_stable_openapi_valid_and_golden(tmp_path: Path) -> None:
    document = _document("normal")
    database = tmp_path / "normal.sqlite"
    _seed_database(database, document)
    with sqlite3.connect(database) as connection:
        projected = build_public_issue(connection, issue_date=cast(str, document["issueDate"]))
        assert projected == document
        json_first = render_daily_json(connection, issue_date=cast(str, document["issueDate"]))
        json_second = render_daily_json(connection, issue_date=cast(str, document["issueDate"]))
        docx_first = render_daily_docx(connection, issue_date=cast(str, document["issueDate"]))
        docx_second = render_daily_docx(connection, issue_date=cast(str, document["issueDate"]))
    assert json_first == json_second
    assert docx_first == docx_second
    assert validate_daily_json(json_first) == document
    report = validate_daily_docx(docx_first, expected_document=document)
    _assert_openapi_issue(document)
    expected = cast(dict[str, object], _golden()["normal"])
    assert hashlib.sha256(json_first).hexdigest() == expected["jsonSha256"]
    assert hashlib.sha256(docx_first).hexdigest() == expected["docxSha256"]
    assert report.text_sha256 == expected["docxTextSha256"]
    assert "PRIVATE DRAFT MUST NOT LEAK" not in json_first.decode()
    assert "PRIVATE DRAFT MUST NOT LEAK" not in report.text


def test_no_llm_daily_publishes_deterministic_fallback_and_golden(tmp_path: Path) -> None:
    document = _document("noLlm")
    database = tmp_path / "no-llm.sqlite"
    _seed_database(database, document, raw_analysis_blocks=[])
    with sqlite3.connect(database) as connection:
        projected = build_public_issue(connection, issue_date=cast(str, document["issueDate"]))
        json_bytes = render_daily_json(connection, issue_date=cast(str, document["issueDate"]))
        docx_bytes = render_daily_docx(connection, issue_date=cast(str, document["issueDate"]))
    assert projected == document
    assert validate_daily_json(json_bytes) == document
    report = validate_daily_docx(docx_bytes, expected_document=document)
    assert "все LLM-провайдеры недоступны" in report.text
    assert "детерминированное представление" in report.text
    expected = cast(dict[str, object], _golden()["noLlm"])
    assert hashlib.sha256(json_bytes).hexdigest() == expected["jsonSha256"]
    assert hashlib.sha256(docx_bytes).hexdigest() == expected["docxSha256"]
    assert report.text_sha256 == expected["docxTextSha256"]
    _assert_openapi_issue(document)


def test_missing_legacy_material_analysis_uses_explicit_deterministic_fallback(
    tmp_path: Path,
) -> None:
    document = _document("normal")
    database = tmp_path / "missing-material-analysis.sqlite"
    _seed_database(database, document)
    second = cast(list[dict[str, object]], document["materials"])[1]
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM material_analysis WHERE material_id = ?",
            (second["id"],),
        )
        connection.commit()
        projected = build_public_issue(connection, issue_date=cast(str, document["issueDate"]))
    expected = copy.deepcopy(document)
    expected_second = cast(list[dict[str, object]], expected["materials"])[1]
    expected_second["llm"] = {"effectiveModel": None, "status": "fallback"}
    assert projected == expected
    _assert_openapi_issue(projected)


def test_date_only_timestamp_is_normalized_only_for_legacy_inferred_rows(
    tmp_path: Path,
) -> None:
    document = _document("normal")
    first_id = cast(str, cast(list[dict[str, object]], document["materials"])[0]["id"])

    legacy_db = tmp_path / "legacy-date.sqlite"
    _seed_database(legacy_db, document)
    with sqlite3.connect(legacy_db) as connection:
        connection.execute(
            "UPDATE issues SET publication_origin = 'legacy_inferred' "
            "WHERE lifecycle_status = 'published'"
        )
        connection.execute(
            "UPDATE materials SET published_at = '2026-08-20' WHERE material_id = ?",
            (first_id,),
        )
        connection.commit()
        projected = build_public_issue(connection, issue_date=cast(str, document["issueDate"]))
    assert projected["publishedAt"] == document["publishedAt"]
    projected_material = cast(list[dict[str, object]], projected["materials"])[0]
    assert projected_material["publishedAt"] == "2026-08-20T00:00:00Z"

    v2_db = tmp_path / "v2-date.sqlite"
    _seed_database(v2_db, document)
    with sqlite3.connect(v2_db) as connection:
        connection.execute(
            "UPDATE materials SET published_at = '2026-08-20' WHERE material_id = ?",
            (first_id,),
        )
        connection.commit()
        with pytest.raises(PublicIssueValidationError, match="published_at"):
            build_public_issue(connection, issue_date=cast(str, document["issueDate"]))


def test_stats_duplicate_date_and_draft_invariants_fail_closed(tmp_path: Path) -> None:
    document = _document("normal")
    invalid_stats = copy.deepcopy(document)
    cast(dict[str, object], invalid_stats["stats"])["included"] = 1
    with pytest.raises(PublicIssueValidationError, match="included"):
        validate_public_issue_document(invalid_stats)

    duplicate_document = copy.deepcopy(document)
    duplicate_materials = cast(list[dict[str, object]], duplicate_document["materials"])
    duplicate_materials[1]["canonicalUrl"] = duplicate_materials[0]["canonicalUrl"]
    with pytest.raises(PublicIssueValidationError, match="duplicate material URL"):
        validate_public_issue_document(duplicate_document)

    duplicate_db = tmp_path / "duplicate.sqlite"
    _seed_database(duplicate_db, document)
    with sqlite3.connect(duplicate_db) as connection:
        connection.execute(
            "UPDATE materials SET canonical_url = ? WHERE material_id = ?",
            (
                cast(list[dict[str, object]], document["materials"])[0]["canonicalUrl"],
                cast(list[dict[str, object]], document["materials"])[1]["id"],
            ),
        )
        connection.commit()
        with pytest.raises(PublicIssueValidationError, match="duplicate material URL"):
            build_public_issue(connection, issue_date=cast(str, document["issueDate"]))

    future_db = tmp_path / "future.sqlite"
    _seed_database(future_db, document)
    first_id = cast(str, cast(list[dict[str, object]], document["materials"])[0]["id"])
    with sqlite3.connect(future_db) as connection:
        connection.execute(
            "UPDATE materials SET published_at = '2026-08-25T00:00:00Z' WHERE material_id = ?",
            (first_id,),
        )
        connection.execute(
            "UPDATE material_quality SET issue_date_delta_days = 5 WHERE material_id = ?",
            (first_id,),
        )
        connection.commit()
        with pytest.raises(PublicIssueValidationError, match="after issue window"):
            build_public_issue(connection, issue_date=cast(str, document["issueDate"]))


def test_json_and_docx_tampering_are_rejected(tmp_path: Path) -> None:
    document = _document("normal")
    database = tmp_path / "artifacts.sqlite"
    _seed_database(database, document)
    with sqlite3.connect(database) as connection:
        json_bytes = render_daily_json(connection, issue_date=cast(str, document["issueDate"]))
        docx_bytes = render_daily_docx(connection, issue_date=cast(str, document["issueDate"]))
    with pytest.raises(ArtifactValidationError, match="canonical"):
        validate_daily_json(json_bytes.replace(b'":', b'": ', 1))
    with pytest.raises(ArtifactValidationError, match="ZIP"):
        validate_daily_docx(docx_bytes[:120])
    modified = io.BytesIO(docx_bytes)
    with zipfile.ZipFile(modified, mode="a") as archive:
        archive.writestr("word/vbaProject.bin", b"synthetic macro marker")
    with pytest.raises(ArtifactValidationError, match="membership"):
        validate_daily_docx(modified.getvalue())


def _gazette_candidate(html: bytes, css: bytes) -> JsonObject:
    assets = (
        ("gazettes/2026-08/index.html", "text/html", html),
        ("gazettes/2026-08/styles.css", "text/css", css),
    )
    return {
        "candidateId": "candidate_gazette_stage6_0001",
        "contractVersion": "1.0.0",
        "createdAt": NOW,
        "expectedBase": {
            "logicalStateHash": "a" * 64,
            "releaseId": "release_stage6_base_0001",
            "sequence": 1,
        },
        "expectedGazette": {"state": "absent"},
        "gazetteId": "gazette_synthetic_2026_08",
        "htmlEntrypoint": "gazettes/2026-08/index.html",
        "idempotencyKey": "idempotency_gazette_stage6_0001",
        "initiator": {
            "actorId": "synthetic-owner",
            "kind": "owner-request",
            "requestId": "request_gazette_stage6_0001",
        },
        "inputAssets": [
            {
                "bytes": len(content),
                "mediaType": media_type,
                "relativePath": path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, media_type, content in assets
        ],
        "llmOutcome": {
            "attempts": [],
            "deterministicFallback": None,
            "effective": None,
            "effectiveAttemptOrder": None,
            "requested": None,
            "status": "not_requested",
        },
        "operation": "gazette",
        "ownerRequestDigest": "b" * 64,
        "period": "2026-08",
        "reason": "Publish synthetic gazette",
        "schemaVersion": 1,
        "title": "Синтетическая газета — август 2026",
    }


def test_gazette_validator_accepts_self_contained_html_with_explicit_link_warnings() -> None:
    fixture = cast(dict[str, object], _golden()["gazette"])
    html = cast(str, fixture["html"]).encode()
    css = cast(str, fixture["css"]).encode()
    assets = {
        "gazettes/2026-08/index.html": html,
        "gazettes/2026-08/styles.css": css,
    }
    report = validate_gazette_candidate(_gazette_candidate(html, css), assets)
    assert report.asset_count == 2
    assert report.local_reference_count == 1
    assert report.external_link_count == 1
    assert list(report.warnings) == fixture["warnings"]
    assert report.entrypoint_sha256 == fixture["entrypointSha256"]


@pytest.mark.parametrize(
    "html_change,css_change,missing_css,error",
    [
        (
            lambda value: value.replace(b"<main>", b"<script>document.write('x')</script><main>"),
            None,
            False,
            "forbidden HTML tag",
        ),
        (
            lambda value: value.replace(
                b'href="styles.css"', b'href="https://cdn.example.test/styles.css"'
            ),
            None,
            False,
            "external asset dependency",
        ),
        (
            lambda value: value.replace(b'href="styles.css"', b'href="../../../outside.css"'),
            None,
            False,
            "escapes package",
        ),
        (
            None,
            lambda value: b'@import "https://cdn.example.test/x.css";\n' + value,
            False,
            "active/internal CSS",
        ),
        (None, None, True, "assets differ"),
    ],
)
def test_gazette_validator_rejects_active_external_traversal_and_incomplete_packages(
    html_change: object,
    css_change: object,
    missing_css: bool,
    error: str,
) -> None:
    fixture = cast(dict[str, object], _golden()["gazette"])
    html = cast(str, fixture["html"]).encode()
    css = cast(str, fixture["css"]).encode()
    if callable(html_change):
        html = cast(bytes, html_change(html))
    if callable(css_change):
        css = cast(bytes, css_change(css))
    assets = {"gazettes/2026-08/index.html": html}
    if not missing_css:
        assets["gazettes/2026-08/styles.css"] = css
    with pytest.raises(GazetteValidationError, match=error):
        validate_gazette_candidate(_gazette_candidate(html, css), assets)


def _legacy_timestamp(value: object) -> str | None:
    if value is None:
        return None
    text = cast(str, value)
    return f"{text}T00:00:00Z" if len(text) == 10 else text


def _legacy_public_document(fixture: Mapping[str, object]) -> JsonObject:
    issue = cast(dict[str, object], fixture["issue"])
    stats = cast(dict[str, object], fixture["stats"])
    daily = cast(dict[str, object], fixture["dailyAnalysis"])
    analysis = cast(dict[str, object], daily["analysis"])
    blocks = [
        {"kind": kind, "text": analysis[key], "title": title}
        for key, kind, title in (
            ("signal", "signals", "Сигнал выпуска"),
            ("why_agpm", "overview", "Значение для AgPM"),
            ("watch_next", "actions", "Что отслеживать дальше"),
        )
        if isinstance(analysis.get(key), str) and analysis[key]
    ]
    materials: list[dict[str, object]] = []
    for raw in cast(list[dict[str, object]], fixture["materials"]):
        rubric_ids = [
            cast(str, cast(dict[str, object], rubric)["rubric_id"])
            for rubric in cast(list[object], raw["rubrics"])
        ]
        materials.append(
            {
                "agpmTakeaway": raw["agpm_takeaway"],
                "brief": raw["brief"],
                "canonicalUrl": raw["canonical_url"],
                "id": raw["id"],
                "issueDate": issue["issue_date"],
                "keyMaterial": bool(raw["key_material"]),
                "llm": {"effectiveModel": None, "status": "fallback"},
                "perimeter": raw["perimeter"],
                "publicationDateStatus": raw["publication_date_status"],
                "publishedAt": _legacy_timestamp(raw["published_at"]),
                "rubrics": rubric_ids,
                "signalScore": raw["signal_score"],
                "signalStrength": raw["signal_strength"],
                "sourceName": raw["source_name"],
                "summary": raw["summary"],
                "theses": raw["theses"],
                "title": raw["title"],
                "trendNotes": raw["trend_notes"],
                "url": raw["url"],
                "verdict": raw["verdict"],
            }
        )
    document: dict[str, object] = {
        "analysis": {
            "blocks": blocks,
            "brief": issue["brief"],
            "headline": daily["headline"],
        },
        "brief": issue["brief"],
        "issueDate": issue["issue_date"],
        "issueNumber": issue["issue_number"],
        "llm": {"effectiveModel": daily["model"], "status": daily["status"]},
        "materialCount": len(materials),
        "materials": materials,
        "publishedAt": None,
        "stats": {
            key: stats[key]
            for key in ("viewed", "included", "cut", "near", "mid", "far", "core", "adjacent")
        },
        "theses": issue["theses"],
        "title": issue["title"],
    }
    return validate_public_issue_document(document)


def test_v2_docx_preserves_historical_legacy_content_on_sanitized_fixture() -> None:
    fixture = cast(dict[str, object], json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8")))
    document = _legacy_public_document(fixture)
    v2_docx = render_public_issue_docx(document)
    v2_text = validate_daily_docx(v2_docx, expected_document=document).text
    issue_date = cast(str, document["issueDate"])
    assert issue_date in v2_text
    stats = cast(dict[str, object], document["stats"])
    assert f"Просмотрено: {stats['viewed']}" in v2_text
    for raw_material in cast(list[dict[str, object]], document["materials"]):
        title = cast(str, raw_material["title"])
        assert title in v2_text
