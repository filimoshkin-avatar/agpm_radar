"""Every table in `kx` must be writable by the role that does the work.

Migration 033 created `chain_passes`, granted the reader its view and forgot the
one line every table-creating migration before it ended with: `GRANT ALL ON
<table> TO radar_kx`. The migration gate went green anyway - it applies the SQL
as an administrator and checks that the schema stands up, not that the working
role can do with a new table the thing the table exists for. Production found
out instead: the perimeter poll ran, printed its JSON and died closing its pass
with "permission denied for table chain_passes", and the timer would have gone
on failing every half hour.

Green means "what ran, ran correctly", not "everything ran". This is the check
that makes the next forgotten grant red here rather than there.

Why a list of exceptions rather than a plain sweep: production reaches its
tables two different ways. Twenty-four of them are owned by `radar_kx` and need
no grant at all; the rest are owned by the administrator and carry an explicit
one. The fixture applies every migration as the administrator, so the owned ones
look ungranted here and are fine there. The list below is that difference,
measured on production on 2026-08-25 - a ratchet, not a wall: a new table
belongs in a migration with its grant, not in this list.
"""

from __future__ import annotations

from conftest import connect

#: Owned by `radar_kx` on production, so writable without a grant. Measured
#: 2026-08-25; every one of them predates the rule this test now enforces.
OWNED_BY_WORKER = frozenset(
    {
        "chunk_embeddings",
        "chunks",
        "claim_evidence",
        "claims",
        "corpus_imports",
        "document_versions",
        "documents",
        "embedding_models",
        "entities",
        "entity_aliases",
        "entity_merges",
        "fetch_attempts",
        "fetch_queue",
        "idea_evidence",
        "idea_scores",
        "ideas",
        "material_documents",
        "metadata",
        "metrics",
        "processing_runs",
        "raw_blobs",
        "relations",
        "source_material_revisions",
        "source_materials",
    }
)

#: Deliberately out of the worker's reach. Keys are issued by the owner through
#: the editor; the serving role stamps one column and the worker touches
#: nothing. Migration 031 says so and means it.
NOT_THE_WORKERS = frozenset({"access_keys"})


def test_every_new_table_is_writable_by_the_worker(migrated_dsn: str) -> None:
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT tables.relname AS table_name,
                   has_table_privilege('radar_kx', tables.oid, 'INSERT') AS may_insert
            FROM pg_class AS tables
            JOIN pg_namespace AS schemas ON schemas.oid = tables.relnamespace
            WHERE schemas.nspname = 'kx' AND tables.relkind = 'r'
            ORDER BY tables.relname
            """
        )
        rows = [dict(row) for row in cursor.fetchall()]

    assert rows, "no tables found in kx - the fixture did not apply the migrations"
    known = OWNED_BY_WORKER | NOT_THE_WORKERS
    mute = sorted(
        str(row["table_name"])
        for row in rows
        if not row["may_insert"] and str(row["table_name"]) not in known
    )
    assert not mute, (
        f"radar_kx cannot write to {mute}. A table the worker cannot fill is a table "
        "nothing fills: add `GRANT ALL ON <table> TO radar_kx;` to the migration that "
        "created it."
    )


def test_the_exception_lists_still_describe_real_tables(migrated_dsn: str) -> None:
    """A name that no longer exists is a note nobody will notice going stale."""
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT tables.relname AS table_name FROM pg_class AS tables"
            " JOIN pg_namespace AS schemas ON schemas.oid = tables.relnamespace"
            " WHERE schemas.nspname = 'kx' AND tables.relkind = 'r'"
        )
        present = {str(dict(row)["table_name"]) for row in cursor.fetchall()}

    gone = sorted((OWNED_BY_WORKER | NOT_THE_WORKERS) - present)
    assert not gone, f"named as exceptions but no longer in the schema: {gone}"
