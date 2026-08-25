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


def test_perimeter_migration_keeps_selections_immutable_and_overrides_reasoned() -> None:
    schema = (Path(__file__).parents[1] / "sql" / "002_issue_perimeter.sql").read_text(
        encoding="utf-8"
    )
    for table in ("issue_perimeter_sources", "issue_perimeter_members", "reparse_runs"):
        assert f"CREATE TABLE {table}" in schema
    assert "issue_perimeter_sources_immutable" in schema
    assert "issue_perimeter_members_immutable" in schema
    assert "reparse_runs_immutable" in schema
    assert "CREATE VIEW issue_perimeter_documents" in schema
    assert "fetch_queue_override_requires_reason" in schema
    assert "CHECK (NOT robots_override OR robots_override_reason IS NOT NULL)" in schema
    # Overridden evidence must stay distinguishable from ordinary robots-respecting evidence.
    assert schema.count("'network', 'network_robots_override'") == 2
    # 001 granted the service role out of band, so later objects must grant explicitly
    # or the deployed worker cannot read or write them.
    assert "GRANT ALL ON issue_perimeter_sources" in schema
    assert "GRANT USAGE, SELECT ON SEQUENCE reparse_runs_reparse_id_seq TO radar_kx" in schema
    assert "GRANT SELECT ON issue_perimeter_documents TO radar_kx" in schema
    assert "UPDATE metadata SET value = '2'::jsonb" in schema


def test_reparse_never_relabels_truncated_legacy_excerpts_as_complete() -> None:
    source = (Path(__file__).parents[1] / "src" / "radar_kx" / "database.py").read_text(
        encoding="utf-8"
    )
    assert "attempts.source_kind <> 'legacy_truncated'" in source
    assert "issue perimeter source counts do not match members" in source


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


def test_migration_003_carries_every_object_the_plan_requires() -> None:
    schema = (Path(__file__).parents[1] / "sql" / "003_provenance_and_publication.sql").read_text(
        encoding="utf-8"
    )
    for table in (
        "version_provenance",
        "source_publication_policy",
        "egress_audit",
        "wiki_blobs",
        "wiki_snapshots",
        "wiki_snapshot_files",
        "store_reconciliation_reports",
    ):
        assert f"CREATE TABLE {table}" in schema
        assert f"{table}_immutable" in schema or table in {
            "source_publication_policy",
        }
    for view in ("version_provenance_current", "version_publication_block"):
        assert f"CREATE VIEW {view}" in schema
    # The taxonomy gains the ladder rungs that had no name, and keeps the one that
    # production acquired by hotfix.
    for kind in (
        "'network_browser_headers'",
        "'browser_render'",
        "'web_archive'",
        "'operator_artifact'",
        "'local_import'",
    ):
        assert schema.count(kind) >= 2
    # DROP ... IF EXISTS is what makes the migration land on a database that already
    # carries the hand-applied ALTER (defect D1).
    assert "DROP CONSTRAINT IF EXISTS fetch_attempts_source_kind_check" in schema
    assert "DROP CONSTRAINT IF EXISTS document_versions_source_kind_check" in schema
    assert "ADD COLUMN source_kind text NOT NULL DEFAULT 'radar_materials'" in schema
    assert "documents_canonical_url_scheme" in schema
    assert "GRANT ALL ON version_provenance" in schema
    assert (
        "GRANT USAGE, SELECT ON SEQUENCE version_provenance_provenance_id_seq TO radar_kx" in schema
    )
    assert "UPDATE metadata SET value = '3'::jsonb" in schema


def test_the_worker_gate_matches_the_migration_it_requires() -> None:
    source = (Path(__file__).parents[1] / "src" / "radar_kx" / "database.py").read_text(
        encoding="utf-8"
    )
    schema = (Path(__file__).parents[1] / "sql" / "003_provenance_and_publication.sql").read_text(
        encoding="utf-8"
    )
    # SCHEMA_VERSION is a hard gate (defect D2): the release refuses to run against
    # a database at any other version, so the two must move together and in the
    # order "database first, release second".
    # The constant tracks what is applied, not what is written: require_schema is a
    # hard gate, so a repository ahead of production cannot be released at all.
    assert "SCHEMA_VERSION = 32" in source
    assert "UPDATE metadata SET value = '3'::jsonb" in schema
    caveat = (Path(__file__).parents[1] / "sql" / "004_publication_caveat.sql").read_text(
        encoding="utf-8"
    )
    assert "UPDATE metadata SET value = '4'::jsonb" in caveat
    # A refusal and a caveat are different answers and must be different views.
    assert "CREATE VIEW version_publication_block" in caveat
    assert "CREATE VIEW version_publication_caveat" in caveat
    independence = (Path(__file__).parents[1] / "sql" / "005_source_independence.sql").read_text(
        encoding="utf-8"
    )
    assert "UPDATE metadata SET value = '5'::jsonb" in independence
    # Two documents citing one press release is a hint, never a cluster on its own
    # (ADR-0007 §10). The type carries the rule: there is no formation method for it.
    assert (
        "formation_method IN ('canonical_text_hash', 'shingle_overlap', 'manual')" in independence
    )
    assert "'shared_cited_primary_source'" in independence


def test_a_network_fetch_can_never_be_recorded_as_an_operator_artifact() -> None:
    source = (Path(__file__).parents[1] / "src" / "radar_kx" / "database.py").read_text(
        encoding="utf-8"
    )
    assert "a fetch may not record source kind" in source
    assert "an offline import may not record source kind" in source
    assert (
        '"operator_artifact"'
        not in source.split("NETWORK_SOURCE_KINDS = frozenset(", 1)[1].split(")", 1)[0]
    )


def test_no_migration_test_asserts_the_newest_schema_version() -> None:
    """A migration owns the line it writes, not whatever production runs today.

    Three migration tests in a row went red for this: they read the version off
    `migrated_dsn` - which is by definition the newest schema - so landing the
    next migration blamed the previous one. The rule is checkable, so it is
    checked here rather than remembered.
    """
    here = Path(__file__).parent
    for test_file in sorted(here.glob("test_migration_*.py")):
        source = test_file.read_text(encoding="utf-8")
        for fixture in ("migrated_dsn", "agent_dsn", "dated_dsn", "judged_dsn"):
            for block in source.split("def test_")[1:]:
                head, _, body = block.partition("\n")
                if fixture in head and "schema_version" in body:
                    raise AssertionError(
                        f"{test_file.name}: a test taking {fixture} reads schema_version; "
                        "apply the migrations up to its own and assert the line it writes"
                    )
