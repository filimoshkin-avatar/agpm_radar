"""Slice 2.5: the authored wiki as concepts, and evidence bound to its statements."""

from __future__ import annotations

import gzip
import io
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import connect
from radar_kx.config import Settings
from radar_kx.database import Database, VersionProvenance
from radar_kx.extraction import Fragment, ProposedClaim, align_all, prompt_sha256
from radar_kx.parser import parse_content
from radar_kx.wiki_import import MIN_STATEMENT_CHARS, is_authored, parse_page
from radar_kx.wiki_snapshot import read_bundle

PAGE = """# Two-layer accountability

Some prologue before any section, which pages do use.

## Purpose

To describe how accountability splits between the run and the programme.

## Core claims

- An agentic run assigns accountability to exactly one named human owner.
- The owner reviews every outcome before release and records the review.
- short

## Заметки

Свободный раздел, которого нет среди шести конвенций.

## Open questions

- What happens when the named owner is unavailable for a long period?

```markdown
## Core claims

- This is an example inside a fence and is not a claim.
```
"""

PROSE_PAGE = """# A page written in prose

## Purpose

This page states its content in sentences rather than in a list, which is what
twenty-four of the authored pages do. Nothing here should become a statement,
because turning prose into statements is a model's job with a person confirming.
"""


def _settings(dsn: str) -> Settings:
    base = Settings.from_environment()
    return Settings(
        **{
            **{field: getattr(base, field) for field in Settings.__dataclass_fields__},
            "dsn": dsn,
            "min_free_bytes": 1024,
            "capacity_path": str(Path(__file__).resolve().parent),
        }
    )


def _bundle(path: Path, files: dict[str, bytes]) -> Path:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0) as handle:
        handle.write(buffer.getvalue())
    path.write_bytes(packed.getvalue())
    return path


WIKI = {
    "wiki/responsibility/two-layer-accountability.md": PAGE.encode("utf-8"),
    "wiki/overview/prose.md": PROSE_PAGE.encode("utf-8"),
    "raw/some-extract.md": b"# A raw extract nobody wrote\n\n- not a statement\n",
    "SCHEMA.md": b"# Conventions\n",
}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_sections_are_kept_in_order_with_an_optional_convention() -> None:
    page = parse_page("wiki/responsibility/two-layer-accountability.md", PAGE)
    headings = [(item.heading, item.convention) for item in page.sections]
    assert headings == [
        ("Two-layer accountability", None),  # the prologue is a section too
        ("Purpose", "purpose"),
        ("Core claims", "core_claims"),
        ("Заметки", None),  # 257 of 297 headings map to nothing, and that is fine
        ("Open questions", "open_questions"),
    ]


def test_only_list_items_under_claim_bearing_sections_become_statements() -> None:
    page = parse_page("wiki/responsibility/two-layer-accountability.md", PAGE)
    statements = [item.statement for item in page.statements]
    assert len(statements) == 3
    assert statements[0].startswith("An agentic run assigns accountability")
    assert any("named owner is unavailable" in item for item in statements)
    # "short" is below the floor: a label is not a statement evidence can support.
    assert not any(len(item) < MIN_STATEMENT_CHARS for item in statements)
    # Nothing from the Purpose section, which carries no claims by convention.
    assert not any("splits between the run" in item for item in statements)


def test_an_example_inside_a_code_fence_is_not_a_claim() -> None:
    page = parse_page("wiki/responsibility/two-layer-accountability.md", PAGE)
    assert not any("example inside a fence" in item.statement for item in page.statements)


def test_a_statements_nature_comes_from_its_section() -> None:
    page = parse_page("wiki/responsibility/two-layer-accountability.md", PAGE)
    natures = {item.claim_nature for item in page.statements}
    assert natures == {"descriptive", "open_question"}


def test_offsets_point_at_the_statement_in_the_page() -> None:
    page = parse_page("wiki/responsibility/two-layer-accountability.md", PAGE)
    for item in page.statements:
        assert PAGE[item.char_start : item.char_end] == item.statement


def test_a_prose_page_yields_no_statements_rather_than_invented_ones() -> None:
    page = parse_page("wiki/overview/prose.md", PROSE_PAGE)
    assert page.statements == ()
    assert len(page.sections) == 2


def test_raw_extracts_and_bookkeeping_are_not_concepts() -> None:
    # 32 of 93 files under agpm/ are immutable extracts nobody wrote. Importing
    # them would put 35 pages into every "without evidence" report that were
    # never statements.
    assert is_authored("wiki/responsibility/two-layer-accountability.md")
    assert not is_authored("raw/some-extract.md")
    assert not is_authored("SCHEMA.md")


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


def _snapshot(database: Database, tmp_path: Path) -> str:
    snapshot = read_bundle(_bundle(tmp_path / "wiki.tar.gz", WIKI), perimeter="agpm")
    database.record_wiki_snapshot(snapshot, recorded_by="test")
    return snapshot.snapshot_id


def test_the_import_takes_only_authored_pages(migrated_dsn: str, tmp_path: Path) -> None:
    database = Database(_settings(migrated_dsn))
    snapshot_id = _snapshot(database, tmp_path)
    outcome = database.import_wiki_concepts(
        snapshot_id=snapshot_id, perimeter="agpm", imported_by="test"
    )
    assert outcome["pages"] == 2
    assert outcome["concepts"] == 2
    assert outcome["versions"] == 2
    assert outcome["statements"] == 3
    # Purpose, Core claims and Open questions on one page; Purpose on the other.
    assert outcome["mappedSections"] == 4


def test_importing_the_same_snapshot_twice_changes_nothing(
    migrated_dsn: str, tmp_path: Path
) -> None:
    database = Database(_settings(migrated_dsn))
    snapshot_id = _snapshot(database, tmp_path)
    database.import_wiki_concepts(snapshot_id=snapshot_id, perimeter="agpm", imported_by="test")
    again = database.import_wiki_concepts(
        snapshot_id=snapshot_id, perimeter="agpm", imported_by="test"
    )
    assert again["alreadyImported"] == 2
    assert again["statements"] == 0
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS total FROM kx.concept_claims")
        assert cursor.fetchone()["total"] == 3  # type: ignore[index]


def test_a_concept_version_cannot_be_rewritten(migrated_dsn: str, tmp_path: Path) -> None:
    database = Database(_settings(migrated_dsn))
    snapshot_id = _snapshot(database, tmp_path)
    database.import_wiki_concepts(snapshot_id=snapshot_id, perimeter="agpm", imported_by="test")
    for statement in (
        "UPDATE kx.concept_versions SET body = 'edited'",
        "DELETE FROM kx.concept_sections",
        "DELETE FROM kx.concept_claims",
    ):
        with (
            connect(migrated_dsn) as connection,
            connection.cursor() as cursor,
            pytest.raises(Exception, match="immutable|reject"),
        ):
            cursor.execute(statement)


# --------------------------------------------------------------------------
# Binding
# --------------------------------------------------------------------------


EVIDENCE_TEXT = (
    "The governance model is explicit about ownership. An agentic run assigns "
    "accountability to exactly one named human owner, and that owner reviews every "
    "outcome before release. The review is recorded in the decision log together "
    "with a timestamp so that anyone can see who approved it and when it happened."
)


def _kx_claim(database: Database, dsn: str) -> None:
    url = "https://example.com/governance"
    parsed = parse_content(
        body=EVIDENCE_TEXT.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        source_url=url,
        min_text_chars=50,
    )
    outcome = database.store_artifact_version(
        canonical_url=url,
        body=EVIDENCE_TEXT.encode("utf-8"),
        parsed=parsed,
        source_kind="local_import",
        fetched_at=datetime(2026, 8, 22, tzinfo=UTC),
        provenance=VersionProvenance(
            source_access_method="local_import",
            provided_by="test",
            provided_at=datetime(2026, 8, 22, tzinfo=UTC),
        ),
        recorded_by="test",
    )
    version_id = str(outcome.version_id)
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT chunk_id, char_start, char_end, text FROM kx.chunks"
            " WHERE version_id = %s ORDER BY ordinal LIMIT 1",
            (version_id,),
        )
        row = cursor.fetchone()
        assert row is not None
    fragment = Fragment(
        version_id=version_id,
        chunk_id=str(row["chunk_id"]),
        char_start=int(cast(int, row["char_start"])),
        char_end=int(cast(int, row["char_end"])),
        text=str(row["text"]),
    )
    database.record_extraction(
        fragment,
        align_all(
            fragment,
            database.canonical_text(version_id),
            (
                ProposedClaim(
                    predicate="assigns accountability",
                    object_text="an agentic run",
                    quote=(
                        "An agentic run assigns accountability to exactly one named human owner"
                    ),
                ),
            ),
        ),
        model="glm-5.2",
        prompt_sha256=prompt_sha256(fragment),
    )


def test_a_statement_is_bound_to_the_span_that_says_it(migrated_dsn: str, tmp_path: Path) -> None:
    database = Database(_settings(migrated_dsn))
    snapshot_id = _snapshot(database, tmp_path)
    database.import_wiki_concepts(snapshot_id=snapshot_id, perimeter="agpm", imported_by="test")
    _kx_claim(database, migrated_dsn)

    outcome = database.bind_concept_evidence(
        snapshot_id=snapshot_id, scope="corpus", created_by="test"
    )
    assert outcome["statements"] == 3
    assert outcome["proposals"] >= 1
    assert outcome["statementsWithProposal"] >= 1

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT evidence.membership_class, evidence.binding_method,"
            " evidence.confirmed_at, claims.statement"
            " FROM kx.concept_evidence AS evidence"
            " JOIN kx.concept_claims AS claims USING (concept_claim_id)"
        )
        rows = [dict(row) for row in cursor.fetchall()]
    assert rows
    assert all(row["binding_method"] == "search_proposed" for row in rows)
    # Proposed, never confirmed. The machine does not decide what a page rests on.
    assert all(row["confirmed_at"] is None for row in rows)
    assert all(row["membership_class"] == "corpus" for row in rows)


def test_the_report_counts_confirmed_evidence_and_not_proposals(
    migrated_dsn: str, tmp_path: Path
) -> None:
    database = Database(_settings(migrated_dsn))
    snapshot_id = _snapshot(database, tmp_path)
    database.import_wiki_concepts(snapshot_id=snapshot_id, perimeter="agpm", imported_by="test")
    _kx_claim(database, migrated_dsn)
    database.bind_concept_evidence(snapshot_id=snapshot_id, scope="corpus", created_by="test")

    before: dict[str, Any] = database.statements_without_evidence(snapshot_id=snapshot_id)
    assert before["statements"] == 3
    assert before["withConfirmedEvidence"] == 0
    assert before["withProposalsOnly"] >= 1

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE kx.concept_evidence SET confirmed_at = clock_timestamp(),"
            " confirmed_by = 'owner'"
        )
    after: dict[str, Any] = database.statements_without_evidence(snapshot_id=snapshot_id)
    assert after["withConfirmedEvidence"] >= 1
    assert after["withProposalsOnly"] == 0
    # byNature, never averaged: an open question and a descriptive statement are
    # not the same kind of thing to be unsupported.
    assert {row["claim_nature"] for row in after["byNature"]} == {
        "descriptive",
        "open_question",
    }
