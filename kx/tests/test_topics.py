"""Slice 2.5в: the owner's backbone, the subjects it puts on things, and what that changes.

The first comparison of the two linking methods could only measure agreement,
because neither score means what it looks like and there is no gold set. These
tests cover the measurement the backbone adds: whether what a method returns is
about the right subject at all.
"""

from __future__ import annotations

import gzip
import io
import json
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
from radar_kx.skeleton import MAX_LEVEL, SkeletonError, load_authored_skeleton
from radar_kx.topics import (
    MAX_TOPICS_PER_ITEM,
    AssignableItem,
    build_rubricator,
    document_item,
    parse_assignment,
)
from radar_kx.wiki_snapshot import read_bundle

SKELETON = Path(__file__).resolve().parents[1] / "data" / "topic-skeleton-authored-2026-08-23.json"

TEST_MODEL = "test-vectors"


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


# --------------------------------------------------------------------------
# Reading the authored composition
# --------------------------------------------------------------------------


def test_the_shipped_backbone_is_the_document_the_owner_wrote() -> None:
    skeleton = load_authored_skeleton(SKELETON)
    # Twelve catalogue sections, their subgroups, and the elements those enumerate.
    assert skeleton.by_level() == {1: 12, 2: 114, 3: 103}
    assert skeleton.decided_by
    keys = [topic.key for topic in skeleton.topics]
    assert len(keys) == len(set(keys))
    # Parents always arrive before their children, which is what lets one pass
    # resolve parent_key against what it has already inserted.
    seen: set[str] = set()
    for topic in skeleton.topics:
        assert topic.parent_key is None or topic.parent_key in seen
        assert (topic.level == 1) == (topic.parent_key is None)
        seen.add(topic.key)


def test_the_sections_that_are_not_subjects_say_why_they_are_not() -> None:
    # The document has four dimensions and only one of them answers "what is this
    # knowledge about". Dropping the other three silently would leave the next
    # reader thinking the backbone is the whole document.
    skeleton = load_authored_skeleton(SKELETON)
    excluded = {item["section"] for item in skeleton.not_loaded}
    assert "00-governance" in excluded
    assert all(item.get("reason") for item in skeleton.not_loaded)


def test_a_skeleton_nobody_signed_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "unsigned.json"
    path.write_text(
        json.dumps({"skeletonKey": "x", "topics": [{"key": "a-topic", "title": "T"}]}),
        encoding="utf-8",
    )
    with pytest.raises(SkeletonError, match="who decided"):
        load_authored_skeleton(path)


def test_one_key_cannot_mean_two_topics(tmp_path: Path) -> None:
    path = tmp_path / "twice.json"
    path.write_text(
        json.dumps(
            {
                "skeletonKey": "x",
                "decidedBy": "owner",
                "topics": [
                    {"key": "risks", "title": "Риски"},
                    {"key": "risks", "title": "Riscs"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SkeletonError, match="twice"):
        load_authored_skeleton(path)


def test_the_backbone_is_three_levels_deep(tmp_path: Path) -> None:
    deep: dict[str, Any] = {"key": "level-four", "title": "T"}
    for level in range(MAX_LEVEL, 0, -1):
        deep = {"key": f"level-{level}", "title": "T", "children": [deep]}
    path = tmp_path / "deep.json"
    path.write_text(
        json.dumps({"skeletonKey": "x", "decidedBy": "owner", "topics": [deep]}), encoding="utf-8"
    )
    with pytest.raises(SkeletonError, match="three levels"):
        load_authored_skeleton(path)


def test_a_key_the_schema_would_reject_is_refused_before_the_database_sees_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cyrillic.json"
    path.write_text(
        json.dumps(
            {"skeletonKey": "x", "decidedBy": "o", "topics": [{"key": "риски", "title": "Р"}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(SkeletonError, match="usable topic key"):
        load_authored_skeleton(path)


# --------------------------------------------------------------------------
# Reading the model's answer
# --------------------------------------------------------------------------


ITEMS = (AssignableItem(key="one", text="a"), AssignableItem(key="two", text="b"))
ALLOWED = frozenset({"risks", "accountability"})


def test_a_key_the_backbone_does_not_have_is_dropped_and_counted() -> None:
    # A model that invents half its keys and a model that answers cleanly must not
    # produce the same-looking result.
    found, dropped = parse_assignment(
        '[{"item": 1, "topics": ["risks", "invented"]}, {"item": 2, "topics": []}]',
        ITEMS,
        ALLOWED,
    )
    assert found[0].topic_keys == ("risks",)
    assert found[1].topic_keys == ()
    assert dropped["unknownTopic"] == 1


def test_an_answer_wrapped_in_a_fence_is_still_an_answer() -> None:
    found, _ = parse_assignment(
        '```json\n[{"item": 1, "topics": ["accountability"]}]\n```', ITEMS, ALLOWED
    )
    assert found[0].key == "one"


def test_an_item_number_nobody_asked_about_is_dropped() -> None:
    found, dropped = parse_assignment('[{"item": 9, "topics": ["risks"]}]', ITEMS, ALLOWED)
    assert found == ()
    assert dropped["unknownItem"] == 1


def test_more_subjects_than_the_cap_would_make_the_restriction_meaningless() -> None:
    allowed = frozenset({f"t{index}" for index in range(6)})
    items = (AssignableItem(key="one", text="a"),)
    answer = json.dumps([{"item": 1, "topics": sorted(allowed)}])
    found, dropped = parse_assignment(answer, items, allowed)
    assert len(found[0].topic_keys) == MAX_TOPICS_PER_ITEM
    assert dropped["overCap"] == len(allowed) - MAX_TOPICS_PER_ITEM


def test_an_answer_that_is_not_a_list_is_refused() -> None:
    from radar_kx.topics import TopicAssignmentError

    with pytest.raises(TopicAssignmentError):
        parse_assignment("I could not do this", ITEMS, ALLOWED)


def test_the_rubricator_carries_the_section_each_topic_belongs_to() -> None:
    text = build_rubricator(
        [{"topic_key": "risks", "title": "Риски", "path": "Ответственность / Риски"}]
    )
    assert "risks — Риски — Ответственность" in text


def test_a_document_reaches_the_model_as_a_title_and_a_lede() -> None:
    item = document_item(document_id="d" * 64, title="  Заголовок\n", lede="x" * 5000)
    assert item.text.startswith("Заголовок — ")
    assert len(item.text) < 400


# --------------------------------------------------------------------------
# The backbone in the store
# --------------------------------------------------------------------------


def test_the_backbone_lands_with_its_shape_intact(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    outcome = database.adopt_authored_skeleton(load_authored_skeleton(SKELETON))
    assert outcome["inserted"] == outcome["topics"] == 229

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT level, count(*) AS total FROM kx.topics GROUP BY level ORDER BY level"
        )
        assert [dict(row) for row in cursor.fetchall()] == [
            {"level": 1, "total": 12},
            {"level": 2, "total": 114},
            {"level": 3, "total": 103},
        ]
        cursor.execute(
            "SELECT child.topic_key, parent.topic_key AS parent"
            " FROM kx.topics AS child JOIN kx.topics AS parent ON parent.topic_id = child.parent_id"
            " WHERE child.topic_key = 'subject-regulator'"
        )
        assert one(cursor)["parent"] == "ontology-subjects"
        # The adoption is an editorial fact with a name on it, not a load script.
        cursor.execute(
            "SELECT actor, verdict FROM kx.editorial_decisions WHERE object_kind = 'topic_skeleton'"
        )
        assert one(cursor)["verdict"] == "confirmed"


def test_reloading_a_corrected_file_does_not_duplicate_a_topic(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    skeleton = load_authored_skeleton(SKELETON)
    database.adopt_authored_skeleton(skeleton)
    again = database.adopt_authored_skeleton(skeleton)
    assert again["inserted"] == 0
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS total FROM kx.topics")
        assert one(cursor)["total"] == 229


def test_a_subject_outside_the_backbone_is_never_written(migrated_dsn: str) -> None:
    from radar_kx.topics import Assignment

    database = Database(_settings(migrated_dsn))
    database.adopt_authored_skeleton(load_authored_skeleton(SKELETON))
    written = database.record_topic_assignments(
        "statement",
        [Assignment(key="00000000-0000-0000-0000-000000000000", topic_keys=("not-a-topic",))],
        assigned_by="test",
    )
    assert written == 0


def one(cursor: Any) -> dict[str, Any]:
    row = cursor.fetchone()
    assert row is not None
    return dict(row)


# --------------------------------------------------------------------------
# What the restriction changes
# --------------------------------------------------------------------------

STATEMENT_PAGE = """# Two-layer accountability

## Core claims

- An agentic run assigns accountability to exactly one named human owner.
"""

ON_TOPIC = (
    "Every agentic run at the programme office assigns accountability to one named "
    "human owner who signs the outcome."
)
OFF_TOPIC = (
    "The regulator published a draft rule on agent registration and named an owner "
    "for each supervisory action."
)


def _wiki_bundle(path: Path) -> Path:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        content = STATEMENT_PAGE.encode("utf-8")
        info = tarfile.TarInfo(name="wiki/responsibility/two-layer-accountability.md")
        info.size = len(content)
        info.mtime = 0
        archive.addfile(info, io.BytesIO(content))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0) as handle:
        handle.write(buffer.getvalue())
    path.write_bytes(packed.getvalue())
    return path


def _document(database: Database, dsn: str, *, url: str, text: str) -> str:
    """Store one document and extract the whole of it as one claim."""
    body = text.encode("utf-8")
    parsed = parse_content(
        body=body, content_type="text/plain; charset=utf-8", source_url=url, min_text_chars=50
    )
    outcome = database.store_artifact_version(
        canonical_url=url,
        body=body,
        parsed=parsed,
        source_kind="local_import",
        fetched_at=datetime(2026, 8, 23, tzinfo=UTC),
        provenance=VersionProvenance(
            source_access_method="local_import",
            provided_by="test",
            provided_at=datetime(2026, 8, 23, tzinfo=UTC),
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
            (ProposedClaim("says", "the document", text),),
        ),
        model="glm-5.2",
        prompt_sha256=prompt_sha256(fragment),
    )
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT claim_id::text AS claim_id FROM kx.claim_evidence AS evidence"
            " WHERE evidence.version_id = %s AND evidence.match_status = 'exact' LIMIT 1",
            (version_id,),
        )
        return str(one(cursor)["claim_id"])


def _vector(dsn: str, kind: str, key: str, vector: str) -> None:
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.embedding_models (model_id, dimensions, provider)"
            " VALUES (%s, 3, 'local') ON CONFLICT DO NOTHING",
            (TEST_MODEL,),
        )
        cursor.execute(
            "INSERT INTO kx.text_embeddings"
            " (owner_kind, owner_key, model_id, text_sha256, embedding)"
            " VALUES (%s, %s, %s, %s, %s::vector)",
            (kind, key, TEST_MODEL, "0" * 64, vector),
        )


@pytest.fixture
def placed(migrated_dsn: str, tmp_path: Path) -> dict[str, Any]:
    """One statement, two documents, and a backbone that separates them."""
    database = Database(_settings(migrated_dsn))
    database.adopt_authored_skeleton(load_authored_skeleton(SKELETON))

    snapshot = read_bundle(_wiki_bundle(tmp_path / "wiki.tar.gz"), perimeter="agpm")
    database.record_wiki_snapshot(snapshot, recorded_by="test")
    database.import_wiki_concepts(
        snapshot_id=snapshot.snapshot_id, perimeter="agpm", imported_by="test"
    )
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT concept_claim_id::text AS id FROM kx.concept_claims")
        statement_id = str(one(cursor)["id"])

    on_topic = _document(
        database, migrated_dsn, url="https://example.com/accountability", text=ON_TOPIC
    )
    off_topic = _document(
        database, migrated_dsn, url="https://example.com/regulator", text=OFF_TOPIC
    )

    # The off-topic quotation is the *closer* vector. Unrestricted, the semantic
    # method must pick it; inside the subject it cannot see it at all.
    _vector(migrated_dsn, "concept_claim", statement_id, "[1,0,0]")
    _vector(migrated_dsn, "claim_evidence", off_topic, "[0.99,0.14,0]")
    _vector(migrated_dsn, "claim_evidence", on_topic, "[0.7,0.71,0]")

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        for table, column, key, topic in (
            ("concept_claim_topics", "concept_claim_id", statement_id, "accountability"),
            ("document_topics", "document_id", None, "accountability"),
        ):
            if key is None:
                continue
            cursor.execute(
                f"INSERT INTO kx.{table} ({column}, topic_id, assigned_by, method)"  # noqa: S608
                " SELECT %s, topic_id, 'test', 'manual' FROM kx.topics WHERE topic_key = %s",
                (key, topic),
            )
        cursor.execute(
            "INSERT INTO kx.document_topics (document_id, topic_id, assigned_by, method)"
            " SELECT versions.document_id, topics.topic_id, 'test', 'manual'"
            " FROM kx.claim_evidence AS evidence"
            " JOIN kx.document_versions AS versions USING (version_id)"
            " CROSS JOIN kx.topics AS topics"
            " WHERE evidence.claim_id::text = %s AND topics.topic_key = %s",
            (on_topic, "accountability"),
        )
        cursor.execute(
            "INSERT INTO kx.document_topics (document_id, topic_id, assigned_by, method)"
            " SELECT versions.document_id, topics.topic_id, 'test', 'manual'"
            " FROM kx.claim_evidence AS evidence"
            " JOIN kx.document_versions AS versions USING (version_id)"
            " CROSS JOIN kx.topics AS topics"
            " WHERE evidence.claim_id::text = %s AND topics.topic_key = %s",
            (off_topic, "domain-regulation"),
        )
    return {
        "database": database,
        "statementId": statement_id,
        "onTopic": on_topic,
        "offTopic": off_topic,
    }


def test_the_subject_restriction_changes_which_quotation_wins(placed: dict[str, Any]) -> None:
    database = cast(Database, placed["database"])
    outcome = database.compare_binding_methods_within_topics(model_id=TEST_MODEL)

    assert outcome["statements"] == 1
    # Unrestricted, the nearest vector belongs to a document about regulation.
    assert outcome["runs"]["semantic"]["firstChoiceOnTopic"] == 0
    # Inside the subject, the only reachable quotation is the right one.
    assert outcome["runs"]["semanticInTopic"]["firstChoiceOnTopic"] == 1
    assert outcome["runs"]["semanticInTopic"]["statementsAnswered"] == 1

    with connect(database.settings.dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT detail FROM kx.binding_method_comparisons ORDER BY ran_at DESC LIMIT 1"
        )
        detail = cast(list[dict[str, Any]], one(cursor)["detail"])
    assert detail[0]["semanticTop"]["claimId"] == placed["onTopic"]
    assert detail[0]["semanticTopAnywhere"] == [placed["offTopic"]]


def test_a_comparison_over_subjects_is_recorded_and_cannot_be_edited(
    placed: dict[str, Any],
) -> None:
    # A comparison is a recorded run, not a printout somebody remembers.
    database = cast(Database, placed["database"])
    database.compare_binding_methods_within_topics(model_id=TEST_MODEL)
    with (
        connect(database.settings.dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(Exception, match="immutable"),
    ):
        cursor.execute("UPDATE kx.binding_method_comparisons SET statements = 0")


def test_what_still_has_no_subject_is_what_gets_offered(placed: dict[str, Any]) -> None:
    database = cast(Database, placed["database"])
    # The statement and both documents were placed by the fixture, so nothing is
    # waiting; a document with no exact quotation is never offered at all.
    assert database.unassigned_topic_items("statement") == []
    assert database.unassigned_topic_items("document") == []
    report = database.topic_assignment_report()
    assert report["statements_placed"] == 1
    assert report["documents"] == report["documents_placed"] == 2


def test_the_owner_sees_their_own_composition_above_the_three_they_rejected(
    migrated_dsn: str,
) -> None:
    from radar_kx.editor_queues import QUEUES

    database = Database(_settings(migrated_dsn))
    database.adopt_authored_skeleton(load_authored_skeleton(SKELETON))
    queue = next(item for item in QUEUES if item.key == "skeleton")
    _, items = queue.load(database, 25)
    assert items[0].item_id == "authored"
    assert len(items[0].children) == 12
    # Nothing to decide: the composition is the owner's own.
    assert items[0].actions == ()


def test_only_the_decisions_that_are_current_are_on_the_wall() -> None:
    # The owner asked for the tabs that need a decision today and nothing else.
    from radar_kx.editor_queues import QUEUES, QUEUES_BY_KEY, RETIRED

    assert [queue.key for queue in QUEUES] == [
        "skeleton",
        "comparison",
        "families",
        "duplicates",
        "ideas",
        "hosts",
    ]
    assert {item.queue.key for item in RETIRED} == {"evidence", "aliases"}
    # Retired means off the wall, not half-off: the API refuses the key too.
    assert all(item.queue.key not in QUEUES_BY_KEY for item in RETIRED)
    # And each one says what would put it back, so this is a decision and not a loss.
    assert all(item.reason and item.returns_when for item in RETIRED)


def test_a_statement_under_three_subjects_is_still_one_statement(placed: dict[str, Any]) -> None:
    # The first version of this report joined the assignments and counted 448 of
    # 233. A denominator that grows with the tagging measures the tagging.
    database = cast(Database, placed["database"])
    with connect(database.settings.dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.concept_claim_topics"
            " (concept_claim_id, topic_id, assigned_by, method)"
            " SELECT %s, topic_id, 'test', 'manual' FROM kx.topics"
            " WHERE topic_key IN ('risks', 'trust-and-control')",
            (placed["statementId"],),
        )
    report = database.topic_assignment_report()
    assert report["statements"] == 1
    assert report["statements_placed"] == 1
