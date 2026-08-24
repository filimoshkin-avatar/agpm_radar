"""Slice 2.12: the review queue, and who is allowed to work it."""

from __future__ import annotations

import base64
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
    SCRIPT,
    STYLE,
    EditorService,
    Throttle,
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
    service = EditorService(
        database, token=TOKEN, actor="owner", username="helen", password="helen"
    )
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
        for path in ("/", "/api/summary", "/api/queue"):
            assert _request(f"{base}{path}", token=None)[0] == 401
            assert _request(f"{base}{path}", token="wrong" * 8)[0] == 401
        assert _request(f"{base}/api/summary")[0] == 200
        status, body = _request(f"{base}/?token={TOKEN}", token=None)
        assert status == 200
        assert "очередь решений" in str(body).lower()
    finally:
        server.shutdown()


def test_the_actor_comes_from_the_token_and_never_from_the_request(
    migrated_dsn: str,
) -> None:
    # A service that accepts a name in a body records whatever the caller felt
    # like being.
    database = Database(_settings(migrated_dsn))
    server, base = _serve(database)
    try:
        status, _ = _request(
            f"{base}/api/decide",
            payload={
                "queue": "hosts",
                "id": "example.com",
                "action": "rejected",
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
        for payload in ({"action": "confirmed"}, {"queue": "evidence", "id": "x"}):
            status, body = _request(f"{base}/api/decide", payload=payload)
            assert status == 400
            assert "error" in body
    finally:
        server.shutdown()


def test_the_page_carries_no_inline_script() -> None:
    # radar.agpm.space serves script-src 'self'. An inline handler or an inline
    # <script> is silently dead under it, and "the buttons do nothing" is a bad
    # way to discover a Content-Security-Policy.
    assert "<!doctype html>" in PAGE
    assert "onclick" not in PAGE
    assert "<script>" not in PAGE
    assert 'src="app.js"' in PAGE
    assert STYLE.strip() and SCRIPT.strip()


def test_basic_auth_is_accepted_and_a_wrong_password_is_not(migrated_dsn: str) -> None:
    service = EditorService(
        Database(_settings(migrated_dsn)),
        token=TOKEN,
        actor="owner",
        username="helen",
        password="helen",
    )
    header = "Basic " + base64.b64encode(b"helen:helen").decode()
    assert service.authorized(header)
    assert not service.authorized("Basic " + base64.b64encode(b"helen:wrong").decode())
    assert not service.authorized("Basic " + base64.b64encode(b"someone:helen").decode())
    assert not service.authorized("Basic not-base64-at-all")
    # The bearer path still works for the loopback and scripted callers.
    assert service.authorized(f"Bearer {TOKEN}")


def test_basic_auth_is_refused_when_no_password_is_configured(migrated_dsn: str) -> None:
    service = EditorService(Database(_settings(migrated_dsn)), token=TOKEN, actor="owner")
    assert not service.authorized("Basic " + base64.b64encode(b"helen:helen").decode())


def test_failed_attempts_are_throttled() -> None:
    # A public address with a short password and no throttle is found by scanners
    # in days. The throttle is the part of that this code can fix.
    throttle = Throttle(limit=3, window=60.0)
    assert not throttle.blocked("1.2.3.4")
    for _ in range(3):
        throttle.record_failure("1.2.3.4", now=100.0)
    assert throttle.blocked("1.2.3.4", now=100.0)
    # Another client is unaffected, and the window expires.
    assert not throttle.blocked("5.6.7.8", now=100.0)
    assert not throttle.blocked("1.2.3.4", now=200.0)


def test_a_document_outside_the_listing_is_not_served(migrated_dsn: str, tmp_path: Path) -> None:
    # The name is matched against what the listing offered, never joined onto a
    # path: a service that resolves a caller's path serves whatever it can name.
    (tmp_path / "radar-kb-good-2026-08-23.md").write_text("# Good\n\ntext\n", encoding="utf-8")
    (tmp_path / "secret.md").write_text("# Secret\n", encoding="utf-8")
    service = EditorService(
        Database(_settings(migrated_dsn)), token=TOKEN, actor="owner", docs_directory=tmp_path
    )
    listed = {item["name"] for item in service.documents()["documents"]}
    assert listed == {"radar-kb-good-2026-08-23.md"}
    assert "<h1>Good</h1>" in service.document("radar-kb-good-2026-08-23.md")["html"]
    for name in ("secret.md", "../../etc/passwd", "/etc/passwd"):
        with pytest.raises(KeyError):
            service.document(name)


def test_an_open_question_is_not_offered_for_binding(migrated_dsn: str) -> None:
    # "Should AgPM define a fourth level?" is not a statement anything can be
    # evidence for, and nine of them were at the head of the first production
    # queue. They are still counted as statements without evidence, which is
    # correct: they do not need any.
    database = Database(_settings(migrated_dsn))
    statement_id, _ = _proposal(database, migrated_dsn)
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE kx.concept_claims SET claim_nature = 'open_question'"
            " WHERE concept_claim_id = %s",
            (statement_id,),
        )
    assert database.evidence_queue()["proposalsWaiting"] == 0


def test_the_queue_is_ordered_by_word_overlap_not_by_retrieval_rank(
    migrated_dsn: str,
) -> None:
    # Reciprocal rank fusion saturates at 2/61, so on the first production queue
    # every top proposal scored 0.0328 and the order was arbitrary. A reviewer
    # shown noise first stops reading.
    from radar_kx.ideas import term_coverage

    statement = "An agentic run assigns accountability to one named human owner."
    close = "the run assigns accountability to exactly one named human owner"
    far = "procurement cycles lengthened after the regulator published guidance"
    assert term_coverage(statement, close) > term_coverage(statement, far)
    assert term_coverage(statement, far) < 0.2


def test_a_host_left_as_it_is_does_not_come_back(migrated_dsn: str) -> None:
    """Only `confirmed` wrote a profile, so the queue remembered a yes and
    forgot every no.

    Found by signing the queues in bulk: 76 hosts collected 2 042 identical
    rejections, because each rejection left the host exactly where it was and
    the next page offered it again as a fresh question. Same shape as the
    linking drift migration 025 fixed - a verdict that changes nothing must
    still be a verdict the queue reads.
    """
    from radar_kx.database import Database

    database = Database(_settings(migrated_dsn))
    before, _ = database.hosts_awaiting_policy(limit=5)
    # `acquisition_gap_queue` is a view over `fetch_queue` and `documents`; the
    # host is read out of the document's URL.
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        document = "c" * 64
        cursor.execute(
            "INSERT INTO kx.documents (document_id, canonical_url) VALUES (%s, %s)",
            (document, "https://blocked.example/a"),
        )
        cursor.execute(
            "INSERT INTO kx.fetch_queue (document_id, status, terminal_reason, last_error_code)"
            " VALUES (%s, 'failed', 'blocked_by_host', 'robots_denied')",
            (document,),
        )
    after_adding, rows = database.hosts_awaiting_policy(limit=50)
    assert after_adding == before + 1
    assert any(str(row["host"]) == "blocked.example" for row in rows)

    database.decide_host_policy(host="blocked.example", verdict="rejected", actor="test")
    after_deciding, rows = database.hosts_awaiting_policy(limit=50)
    assert after_deciding == before, "a host she decided on is still being offered"
    assert not any(str(row["host"]) == "blocked.example" for row in rows)


def test_the_count_on_a_queue_and_its_list_agree(migrated_dsn: str) -> None:
    """Every queue answers twice - a page and a total - from two queries.

    A filter added to one and not the other gives a tab that announces work and
    shows none, which is how the wiki queue once reported 0 and how the hosts
    queue kept announcing 86 after the page had emptied.
    """
    from radar_kx.database import Database
    from radar_kx.editor_queues import QUEUES

    database = Database(_settings(migrated_dsn))
    for queue in QUEUES:
        total, rows = queue.load(database, 200)
        assert total >= len(rows), f"{queue.key}: the page holds more than the count admits"
        if total <= 200:
            assert total == len(rows), (
                f"{queue.key}: says {total} and lists {len(rows)} - the count query and the "
                "page query do not apply the same filters"
            )


def test_the_wall_holds_only_what_awaits_a_decision(migrated_dsn: str) -> None:
    """Four queues answer a question; one shows what is already in force.

    Four more were retired on 2026-08-24 because the decisions they served had
    been made elsewhere: the linking-method comparison was settled by its own
    220 votes, source families and duplicate clusters were superseded by
    counting primary sources rather than publishers, and the idea layer was
    overtaken by the subject backbone it predates.
    """
    from radar_kx.editor_queues import QUEUES, RETIRED

    deciding = [queue.key for queue in QUEUES if not queue.reference]
    assert deciding == ["wiki", "promotion", "freshness", "hosts"]
    assert [queue.key for queue in QUEUES if queue.reference] == ["skeleton"]

    retired = {entry.queue.key for entry in RETIRED}
    assert {"comparison", "families", "duplicates", "ideas"} <= retired

    # Retirement is not deletion: each one still loads, and each says what would
    # put it back, so nobody has to reconstruct the reasoning from a git log.
    for entry in RETIRED:
        assert entry.queue.load is not None
        assert entry.reason.strip()
        assert entry.returns_when.strip()


def test_a_reference_tab_is_not_counted_as_pending(migrated_dsn: str) -> None:
    """It will always read zero, and a zero among pending work trains the eye."""
    from radar_kx.database import Database
    from radar_kx.editor_queues import queue_summary

    summary = queue_summary(Database(_settings(migrated_dsn)))
    reference = [row for row in summary if row["reference"]]
    assert len(reference) == 1
    assert reference[0]["key"] == "skeleton"
    assert all("reference" in row for row in summary), "the client cannot tell them apart"
