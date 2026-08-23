"""Slice 2.12: the review queue, and who is allowed to work it."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import connect
from radar_kx.config import Settings
from radar_kx.database import Database, VersionProvenance
from radar_kx.editor_service import (
    PAGE,
    EditorService,
    generate_token,
    make_handler,
)
from radar_kx.extraction import Fragment, ProposedClaim, align_all, prompt_sha256
from radar_kx.parser import parse_content

PARAGRAPH = (
    "An agentic run assigns accountability to exactly one named human owner, and "
    "the owner reviews every outcome before release."
)
DOCUMENT = f"{PARAGRAPH}\n\nA second paragraph so the first has a boundary."
TOKEN = "a" * 40


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
# Authorization
# --------------------------------------------------------------------------


def test_a_short_token_is_refused_at_construction(migrated_dsn: str) -> None:
    # A short token on a service that reads other people's full text is not a
    # token, it is a speed bump.
    with pytest.raises(ValueError, match="at least 24 characters"):
        EditorService(Database(_settings(migrated_dsn)), token="secret", actor="owner")


def test_only_the_right_token_is_authorized(migrated_dsn: str) -> None:
    service = EditorService(Database(_settings(migrated_dsn)), token=TOKEN, actor="owner")
    assert service.authorized(f"Bearer {TOKEN}")
    assert not service.authorized(f"Bearer {'a' * 39}b")
    assert not service.authorized(TOKEN)
    assert not service.authorized(None)
    assert not service.authorized("")


def test_a_generated_token_is_long_enough_to_be_one() -> None:
    assert len(generate_token()) >= 40


# --------------------------------------------------------------------------
# The queue
# --------------------------------------------------------------------------


def _proposal(database: Database, dsn: str) -> tuple[str, str]:
    """A wiki statement with one proposed binding waiting for a decision."""
    url = "https://example.com/governance"
    parsed = parse_content(
        body=DOCUMENT.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        source_url=url,
        min_text_chars=50,
    )
    outcome = database.store_artifact_version(
        canonical_url=url,
        body=DOCUMENT.encode("utf-8"),
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
        chunk = cursor.fetchone()
        assert chunk is not None
    fragment = Fragment(
        version_id=version_id,
        chunk_id=str(chunk["chunk_id"]),
        char_start=int(cast(int, chunk["char_start"])),
        char_end=int(cast(int, chunk["char_end"])),
        text=str(chunk["text"]),
    )
    database.record_extraction(
        fragment,
        align_all(
            fragment,
            database.canonical_text(version_id),
            (ProposedClaim("assigns", "an agentic run", PARAGRAPH),),
        ),
        model="glm-5.2",
        prompt_sha256=prompt_sha256(fragment),
    )
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT claim_id FROM kx.claims LIMIT 1")
        claim = cursor.fetchone()
        assert claim is not None
        cursor.execute(
            "INSERT INTO kx.wiki_snapshots (snapshot_id, taken_at, manifest_sha256,"
            " perimeter, file_count, total_bytes, recorded_by)"
            " VALUES ('wiki-test', clock_timestamp(), %s, 'agpm', 1, 1, 'test')",
            ("b" * 64,),
        )
        cursor.execute(
            "INSERT INTO kx.concepts (relative_path, perimeter, layer, created_by)"
            " VALUES ('wiki/a.md', 'agpm', 'synthesis_page', 'test') RETURNING concept_id"
        )
        concept = cursor.fetchone()
        assert concept is not None
        cursor.execute(
            "INSERT INTO kx.concept_versions (concept_version_id, concept_id, snapshot_id,"
            " title, body, body_sha256, word_count, language, imported_by)"
            " VALUES (%s, %s, 'wiki-test', 'A page', 'body', %s, 1, 'en', 'test')",
            ("c" * 64, concept["concept_id"], "d" * 64),
        )
        cursor.execute(
            "INSERT INTO kx.concept_sections (concept_version_id, ordinal, heading,"
            " heading_level, char_start, char_end) VALUES (%s, 0, 'Core claims', 2, 0, 4)"
            " RETURNING section_id",
            ("c" * 64,),
        )
        section = cursor.fetchone()
        assert section is not None
        cursor.execute(
            "INSERT INTO kx.concept_claims (concept_version_id, section_id, ordinal,"
            " char_start, char_end, statement, statement_sha256, claim_nature, segmentation)"
            " VALUES (%s, %s, 0, 0, 4, 'Accountability is named.', %s, 'descriptive',"
            " 'list_item') RETURNING concept_claim_id",
            ("c" * 64, section["section_id"], "e" * 64),
        )
        statement = cursor.fetchone()
        assert statement is not None
        cursor.execute(
            "INSERT INTO kx.concept_evidence (concept_claim_id, claim_id, membership_class,"
            " binding_method, relevance, created_by)"
            " VALUES (%s, %s, 'historical', 'search_proposed', 0.031, 'test')",
            (statement["concept_claim_id"], claim["claim_id"]),
        )
    return str(statement["concept_claim_id"]), str(claim["claim_id"])


def test_the_queue_groups_proposals_under_the_statement_they_are_about(
    migrated_dsn: str,
) -> None:
    # The decision a reviewer makes is about a statement: "which of these, if
    # any, is what this sentence rests on". A flat list of 2 769 proposals is the
    # same information arranged so nobody can act on it.
    database = Database(_settings(migrated_dsn))
    statement_id, claim_id = _proposal(database, migrated_dsn)
    queue: dict[str, Any] = database.evidence_queue()
    assert queue["statementsWaiting"] == 1
    assert queue["proposalsWaiting"] == 1
    item = queue["items"][0]
    assert item["conceptClaimId"] == statement_id
    assert item["statement"] == "Accountability is named."
    proposal = item["proposals"][0]
    assert proposal["claimId"] == claim_id
    # Everything a reviewer needs beside the statement: the quotation, where it
    # came from and the exact offsets.
    assert PARAGRAPH in proposal["quote"]
    assert proposal["sourceUrl"] == "https://example.com/governance"
    assert proposal["charStart"] >= 0


def test_a_decision_leaves_the_queue_and_lands_in_the_journal(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    statement_id, claim_id = _proposal(database, migrated_dsn)
    database.decide_binding(
        concept_claim_id=statement_id,
        claim_id=claim_id,
        verdict="confirmed",
        actor="owner",
        rationale="it says exactly that",
    )
    assert database.evidence_queue()["proposalsWaiting"] == 0
    history = database.editorial_history()
    assert len(history) == 1
    assert history[0]["verdict"] == "confirmed"
    assert history[0]["actor"] == "owner"
    assert history[0]["scope"] == "editor"
    assert history[0]["object_key"] == f"{statement_id}/{claim_id}"


def test_a_rejection_is_a_decision_and_not_an_absence(migrated_dsn: str) -> None:
    # Without somewhere to record it, a reviewer who looked and said no leaves the
    # same trace as one who never opened it, and the queue offers it again tomorrow.
    database = Database(_settings(migrated_dsn))
    statement_id, claim_id = _proposal(database, migrated_dsn)
    database.decide_binding(
        concept_claim_id=statement_id, claim_id=claim_id, verdict="rejected", actor="owner"
    )
    assert database.evidence_queue()["proposalsWaiting"] == 0
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT rejected_by, confirmed_at FROM kx.concept_evidence")
        row = cursor.fetchone()
        assert row is not None
        assert row["rejected_by"] == "owner"
        assert row["confirmed_at"] is None


def test_the_same_binding_cannot_be_decided_twice(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    statement_id, claim_id = _proposal(database, migrated_dsn)
    database.decide_binding(
        concept_claim_id=statement_id, claim_id=claim_id, verdict="confirmed", actor="owner"
    )
    with pytest.raises(ValueError, match="not waiting for a decision"):
        database.decide_binding(
            concept_claim_id=statement_id, claim_id=claim_id, verdict="rejected", actor="owner"
        )


def test_a_decision_cannot_be_rewritten(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    statement_id, claim_id = _proposal(database, migrated_dsn)
    database.decide_binding(
        concept_claim_id=statement_id, claim_id=claim_id, verdict="confirmed", actor="owner"
    )
    with (
        connect(migrated_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(Exception, match="immutable|reject"),
    ):
        cursor.execute("UPDATE kx.editorial_decisions SET actor = 'somebody else'")


# --------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------


def _serve(database: Database) -> tuple[ThreadingHTTPServer, str]:
    service = EditorService(database, token=TOKEN, actor="owner")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _request(url: str, *, token: str | None = TOKEN, payload: Any = None) -> tuple[int, Any]:
    # The URL is built in this file and always points at the loopback server the
    # test just started; S310 is about untrusted schemes.
    request = urllib.request.Request(url)  # noqa: S310
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(payload).encode("utf-8")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            body = response.read()
            return response.status, (
                json.loads(body)
                if response.headers.get_content_type() == "application/json"
                else body.decode("utf-8")
            )
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_every_endpoint_including_the_page_requires_the_token(migrated_dsn: str) -> None:
    # ADR-0006 §7: server-side on every privileged endpoint. A URL is not a
    # credential, and the page itself is the editor interface.
    database = Database(_settings(migrated_dsn))
    server, base = _serve(database)
    try:
        for path in ("/", "/api/queue", "/api/history"):
            assert _request(f"{base}{path}", token=None)[0] == 401
            assert _request(f"{base}{path}", token="wrong" * 8)[0] == 401
        assert _request(f"{base}/api/queue")[0] == 200
        status, body = _request(f"{base}/?token={TOKEN}", token=None)
        assert status == 200
        assert "очередь редактора" in str(body).lower()
    finally:
        server.shutdown()


def test_the_actor_comes_from_the_token_and_never_from_the_request(
    migrated_dsn: str,
) -> None:
    # A service that accepts a name in a body records whatever the caller felt
    # like being.
    database = Database(_settings(migrated_dsn))
    statement_id, claim_id = _proposal(database, migrated_dsn)
    server, base = _serve(database)
    try:
        status, _ = _request(
            f"{base}/api/decide",
            payload={
                "conceptClaimId": statement_id,
                "claimId": claim_id,
                "verdict": "confirmed",
                "actor": "somebody else",
            },
        )
        assert status == 200
    finally:
        server.shutdown()
    assert database.editorial_history()[0]["actor"] == "owner"


def test_a_malformed_decision_is_refused_without_a_traceback(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    server, base = _serve(database)
    try:
        for payload in ({"verdict": "confirmed"}, {"conceptClaimId": "x", "claimId": "y"}):
            status, body = _request(f"{base}/api/decide", payload=payload)
            assert status == 400
            assert "error" in body
    finally:
        server.shutdown()


def test_the_page_is_served_from_its_own_file() -> None:
    # So it can be edited without touching the service, and so a Python linter
    # does not argue with Russian interface text.
    assert "<!doctype html>" in PAGE
    assert (Path(__file__).parents[1] / "src" / "radar_kx" / "editor_page.html").is_file()
