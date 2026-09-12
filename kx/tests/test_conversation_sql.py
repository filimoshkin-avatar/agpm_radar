"""Exercise the serving role and cache keys against schema 35, without a migration."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from conftest import connect, seed_statement
from radar_kx.config import Settings
from radar_kx.database import Database
from radar_kx.research import EvidenceElement
from radar_kx.search import AGENT_FILTERS, AGENT_SEARCH_SQL


def test_serving_role_restricts_sources_before_ranking(migrated_dsn: str) -> None:
    urls = ["https://example.org/radar", "agpm-canon:/test-canon.md", "https://example.org/other"]
    claims = [
        seed_statement(migrated_dsn, url=url, published_on="2026-09-01", first_seen_on="2026-09-01")
        for url in urls
    ]
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        for claim in claims:
            cursor.execute(
                "INSERT INTO kx.claim_evidence (claim_id,version_id,char_start,char_end,"
                "quote_text,quote_sha256,match_status) "
                "SELECT claim_id,version_id,0,4,'text',%s,'exact' FROM kx.claims WHERE claim_id=%s",
                (hashlib.sha256(b"text").hexdigest(), claim),
            )
        cursor.execute(
            "INSERT INTO kx.issue_perimeter_sources (perimeter_source_id,"
            "source_kind,source_reference,"
            "source_sha256,captured_at,row_count,document_count) "
            "VALUES ('scope-test','v2_content_release','test',%s,now(),1,1)",
            ("a" * 64,),
        )
        cursor.execute(
            "INSERT INTO kx.issue_perimeter_members (perimeter_source_id,issue_id,material_ref,"
            "document_id,issue_date,sort_order,perimeter,source_url,"
            "canonical_url,payload,payload_sha256) "
            "SELECT 'scope-test','issue_20260901','material_test',document_id,"
            "'2026-09-01',1,'near',canonical_url,canonical_url,'{}',%s "
            "FROM kx.documents WHERE canonical_url=%s",
            ("b" * 64, urls[0]),
        )
        cursor.execute("SET ROLE radar_kb_public")
        params = dict(
            question="text",
            rrf_k=60,
            limit=8,
            question_vector=None,
            embedding_model="test",
            semantic_depth=50,
            **dict.fromkeys(AGENT_FILTERS),
        )
        for scope, expected in [
            ("radar", {urls[0]}),
            ("canon", {urls[1]}),
            ("radar_canon", set(urls[:2])),
            ("all", set(urls)),
            ("non_radar", set(urls[1:])),
        ]:
            cursor.execute(AGENT_SEARCH_SQL, params | {"corpus": scope})
            assert {row["source_url"] for row in cursor.fetchall()} == expected


def test_cache_context_uses_existing_schema_and_keeps_original_question(migrated_dsn: str) -> None:
    settings = replace(
        Settings.from_environment(),
        dsn=migrated_dsn,
        min_free_bytes=1024,
        capacity_path=str(Path(__file__).parent),
    )
    store = Database(settings)
    evidence = (
        EvidenceElement(
            ordinal=1,
            claim_id="c1",
            quote_text="text",
            source_url="https://example.org",
            char_start=0,
            char_end=4,
            relevance=0.1,
        ),
    )
    for context, answer in [
        (None, "Legacy"),
        ("radar-context", "Статьи"),
        ("canon-context", "Канон"),
    ]:
        store.record_answer(
            question="Сравни их",
            scope="public",
            mode="strict",
            package=evidence,
            answer_text=answer,
            answered_by="test",
            cache_context=context,
        )
    for context, expected in [("radar-context", "Статьи"), ("canon-context", "Канон")]:
        result = store.cached_answer("Сравни их", scope="public", cache_context=context)
        assert result is not None and result["answer_text"] == expected
        assert result["question"] == "Сравни их"
    assert store.cached_answer("Сравни их", scope="public", cache_context="other-history") is None
