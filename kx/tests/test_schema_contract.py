from __future__ import annotations

from pathlib import Path


def test_schema_contains_evidence_and_immutability_contracts() -> None:
    schema = (Path(__file__).parents[1] / "sql" / "001_initial.sql").read_text(encoding="utf-8")
    for table in (
        "raw_blobs",
        "source_material_revisions",
        "document_versions",
        "fetch_queue",
        "processing_runs",
        "claim_evidence",
        "metrics",
        "relations",
        "idea_scores",
    ):
        assert f"CREATE TABLE {table}" in schema
    assert "raw_blobs_immutable" in schema
    assert "document_versions_immutable" in schema
    assert "source_material_revisions_immutable" in schema
    assert "claim_evidence_exact_span" in schema
    chunks_section = schema.split("CREATE TABLE chunks", 1)[1]
    assert "search_ru tsvector GENERATED ALWAYS" in chunks_section
    assert "search_en tsvector GENERATED ALWAYS" in chunks_section


def test_ingest_unit_uses_bounded_thirty_two_worker_pool() -> None:
    unit = (Path(__file__).parents[1] / "deploy" / "radar-kx-ingest.service").read_text(
        encoding="utf-8"
    )
    assert "radar_kx run --workers 32" in unit


def test_ingest_unit_blocks_non_public_network_destinations() -> None:
    unit = (Path(__file__).parents[1] / "deploy" / "radar-kx-ingest.service").read_text(
        encoding="utf-8"
    )
    assert "IPAddressAllow=127.0.0.53/32" in unit
    for network in (
        "IPAddressDeny=10.0.0.0/8",
        "IPAddressDeny=127.0.0.0/8",
        "IPAddressDeny=169.254.0.0/16",
        "IPAddressDeny=172.16.0.0/12",
        "IPAddressDeny=192.168.0.0/16",
        "IPAddressDeny=fc00::/7",
        "IPAddressDeny=fe80::/10",
    ):
        assert network in unit


def test_backup_script_uses_pg_dump_flags_and_guards_missing_partial() -> None:
    script = (Path(__file__).parents[1] / "deploy" / "radar-kx-backup").read_text(encoding="utf-8")
    assert "pg_dump -X" not in script
    assert "pg_dump --format=custom" in script
    assert 'if [ -e "${partial_dump}" ]; then' in script


def test_full_verifier_covers_chunks_documents_revisions_and_corpus_counts() -> None:
    source = (Path(__file__).parents[1] / "src" / "radar_kx" / "database.py").read_text(
        encoding="utf-8"
    )
    assert "one or more chunks violate offset/text/hash continuity" in source
    assert "one or more versions lack complete chunk coverage" in source
    assert "document id mismatch" in source
    assert "material revision payload hash mismatch" in source
    assert "corpus import counts do not match immutable revisions" in source


def test_ready_queue_order_is_source_diverse_not_import_timestamp_order() -> None:
    source = (Path(__file__).parents[1] / "src" / "radar_kx" / "database.py").read_text(
        encoding="utf-8"
    )
    assert "ORDER BY queue.priority DESC, queue.document_id" in source
    assert "ORDER BY priority DESC, next_attempt_at, document_id" not in source
    assert "host_load AS" in source
    assert "row_number() OVER" in source
    assert "host_load.running_count" in source
    assert "attempt_count = greatest(attempt_count - 1, 0)" in source
