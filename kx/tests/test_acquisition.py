"""Slice 2.3: a failure is a routing signal, and the ladder says whose move it is."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import connect
from radar_kx.acquisition import (
    AUTOMATIC_RUNGS,
    DEFAULT_RUNGS,
    ESCALATION_HINT,
    LADDER,
    TERMINAL_BY_ERROR,
    TRANSIENT_ERRORS,
    AcquisitionError,
    HostProfile,
    next_step,
    profile_for,
)
from radar_kx.config import Settings
from radar_kx.database import Database
from radar_kx.web_archive import (
    Snapshot,
    WebArchiveError,
    find_snapshot,
    parse_availability,
)


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
# Profiles
# --------------------------------------------------------------------------


def test_a_host_nobody_decided_about_behaves_exactly_as_it_does_today() -> None:
    # The subsystem is inert until somebody writes a profile. A change to how
    # other people's servers are treated should be a decision, not a deployment.
    profile = profile_for("https://example.com/a", {})
    assert profile.rungs == DEFAULT_RUNGS == ("network",)
    assert profile.robots_policy == "respect"
    assert not profile.allows("network_robots_override")


def test_a_profile_covers_one_host_and_not_its_relatives() -> None:
    # Inheriting a robots override down a domain tree is how one decision quietly
    # becomes twenty.
    reddit = HostProfile(
        host="reddit.com",
        rungs=("network", "network_robots_override"),
        robots_policy="override_recorded",
        rationale="the owner decided on 2026-08-22 that this host is fetched anyway",
        decided_by="owner",
    )
    profiles = {"reddit.com": reddit}
    assert profile_for("https://reddit.com/r/x", profiles).allows("network_robots_override")
    assert not profile_for("https://old.reddit.com/r/x", profiles).allows("network_robots_override")


def test_an_unknown_rung_is_refused_rather_than_ignored() -> None:
    with pytest.raises(AcquisitionError, match="unknown rungs"):
        HostProfile(host="example.com", rungs=("network", "telepathy"))


def test_an_empty_ladder_is_a_decision() -> None:
    profile = HostProfile(host="example.com", rungs=(), rationale="do not fetch", decided_by="o")
    step = next_step(profile=profile, tried=[], error_code=None)
    assert step.is_terminal
    assert step.terminal_reason == "refused_by_policy"
    assert step.next_action_owner == "owner"


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_code", "reason", "owner"),
    [
        ("http_404", "removed_at_source", "machine"),
        ("http_410", "removed_at_source", "machine"),
        ("http_401", "requires_credentials", "owner"),
        ("http_451", "no_public_text", "owner"),
        ("http_407", "requires_credentials", "owner"),
    ],
)
def test_some_failures_mean_something_and_end_the_ladder(
    error_code: str, reason: str, owner: str
) -> None:
    # "failed" says what happened. These say what we now believe, which is the
    # question a gap queue is for.
    step = next_step(profile=HostProfile(host="x"), tried=["network"], error_code=error_code)
    assert step.is_terminal
    assert step.terminal_reason == reason
    assert step.next_action_owner == owner


def test_a_robots_denial_without_a_recorded_override_waits_for_a_decision() -> None:
    # P11 made robots a routing signal, but only where somebody decided that for a
    # host and said why. Until then it is a decision waiting, not a dead end.
    step = next_step(
        profile=HostProfile(host="reddit.com"), tried=["network"], error_code="robots_denied"
    )
    assert step.is_terminal
    assert step.terminal_reason == "blocked_by_host"
    assert step.next_action_owner == "owner"
    assert "no recorded override" in step.detail


def test_a_robots_denial_with_a_recorded_override_escalates() -> None:
    profile = HostProfile(
        host="reddit.com",
        rungs=("network", "network_robots_override"),
        robots_policy="override_recorded",
        rationale="recorded by the owner with a reason on 2026-08-22",
        decided_by="owner",
    )
    step = next_step(profile=profile, tried=["network"], error_code="robots_denied")
    assert step.rung == "network_robots_override"
    assert not step.is_terminal


def test_a_host_declining_this_client_is_not_the_document_being_unavailable() -> None:
    profile = HostProfile(
        host="x.example",
        rungs=("network", "network_browser_headers"),
        rationale="tries browser headers",
        decided_by="o",
    )
    for error_code in ("http_403", "http_429"):
        step = next_step(profile=profile, tried=["network"], error_code=error_code)
        assert step.rung == "network_browser_headers"
    # And with no profile it is one decision away, not a page to read.
    default = next_step(
        profile=HostProfile(host="x.example"), tried=["network"], error_code="http_403"
    )
    assert default.terminal_reason == "blocked_by_host"
    assert default.next_action_owner == "owner"
    assert "network_browser_headers" in default.detail
    # A 5xx is the host having a bad minute, not the host declining this client.
    assert (
        next_step(profile=profile, tried=["network"], error_code="http_503").terminal_reason
        == "transient_exhausted"
    )


def test_a_rung_the_fetcher_cannot_climb_stops_and_names_a_person() -> None:
    # Browser rendering has no unit yet (defect D7) and an operator artifact is a
    # person handing us a file. Neither is a failure of the document.
    profile = HostProfile(
        host="x.example",
        rungs=("network", "browser_render"),
        rationale="only a browser gets this",
        decided_by="o",
    )
    step = next_step(profile=profile, tried=["network"], error_code="empty_body")
    assert step.is_terminal
    assert step.terminal_reason == "ladder_exhausted"
    assert step.next_action_owner == "operator"
    assert "browser_render" in step.detail
    assert "browser_render" not in AUTOMATIC_RUNGS


def test_every_rung_of_the_plan_is_in_the_ladder() -> None:
    assert LADDER == (
        "network",
        "network_browser_headers",
        "network_robots_override",
        "source_specific_parse",
        "browser_render",
        "web_archive",
        "operator_artifact",
    )


# --------------------------------------------------------------------------
# The web archive
# --------------------------------------------------------------------------


AVAILABLE = {
    "archived_snapshots": {
        "closest": {
            "available": True,
            "url": "http://web.archive.org/web/20250104120000/https://example.com/a",
            "timestamp": "20250104120000",
            "status": "200",
        }
    }
}


def test_a_capture_is_read_with_its_date_and_its_address() -> None:
    snapshot = parse_availability("https://example.com/a", AVAILABLE)
    assert isinstance(snapshot, Snapshot)
    assert snapshot.captured_at == datetime(2025, 1, 4, 12, 0, tzinfo=UTC)
    # https, because a citation should not send a reader over plain http.
    assert snapshot.snapshot_url.startswith("https://web.archive.org/")


def test_no_capture_is_not_an_error() -> None:
    assert parse_availability("https://example.com/a", {"archived_snapshots": {}}) is None
    assert (
        parse_availability(
            "https://example.com/a",
            {"archived_snapshots": {"closest": {"available": False}}},
        )
        is None
    )


@pytest.mark.parametrize(
    "closest",
    [
        {"available": True, "timestamp": "20250104120000"},
        {"available": True, "url": "http://web.archive.org/web/x/https://example.com/a"},
    ],
)
def test_a_capture_that_cannot_be_identified_is_refused(closest: dict[str, Any]) -> None:
    # ADR-0004 rule 21: a snapshot is citable because a reader can go and look at
    # the same bytes. Half an identifier reads like evidence and is not.
    with pytest.raises(WebArchiveError, match="cannot be identified"):
        parse_availability("https://example.com/a", {"archived_snapshots": {"closest": closest}})


def test_the_archive_is_asked_once_and_its_answer_is_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["url"] == "https://example.com/a"
        return httpx.Response(200, json=AVAILABLE)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshot = find_snapshot("https://example.com/a", client=client)
    assert snapshot is not None
    assert snapshot.status_code == 200


def test_an_archive_that_is_down_is_an_error_not_a_missing_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(WebArchiveError, match="503"),
    ):
        find_snapshot("https://example.com/a", client=client)


# --------------------------------------------------------------------------
# The gap queue
# --------------------------------------------------------------------------


def _queued(dsn: str, url: str, *, error_code: str) -> str:
    from radar_kx.identifiers import document_id
    from radar_kx.url_policy import canonical_identity_url

    identifier = document_id(canonical_identity_url(url))
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.documents (document_id, canonical_url) VALUES (%s, %s)",
            (identifier, url),
        )
        cursor.execute(
            "INSERT INTO kx.fetch_queue (document_id, status, attempt_count, last_error_code)"
            " VALUES (%s, 'failed', 3, %s)",
            (identifier, error_code),
        )
    return identifier


def test_the_planner_records_a_reason_and_an_owner_for_every_gap(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    _queued(migrated_dsn, "https://gone.example/a", error_code="http_404")
    _queued(migrated_dsn, "https://reddit.com/r/x", error_code="robots_denied")

    planned = database.plan_acquisition()
    assert planned["considered"] == 2
    assert planned["terminal"] == 2
    assert planned["byReason"] == {"removed_at_source": 1, "blocked_by_host": 1}

    gaps: dict[str, Any] = database.acquisition_gaps()
    assert gaps["documentsWithoutText"] == 2
    owners = {row["owner"] for row in gaps["byReason"]}
    assert owners == {"machine", "owner"}


def test_a_written_profile_turns_a_dead_end_into_a_next_rung(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    _queued(migrated_dsn, "https://reddit.com/r/x", error_code="robots_denied")
    database.write_host_profile(
        HostProfile(
            host="reddit.com",
            rungs=("network", "network_robots_override"),
            robots_policy="override_recorded",
            rationale="the owner decided on 2026-08-22 that this host is fetched anyway",
            decided_by="owner",
        )
    )
    planned = database.plan_acquisition()
    assert planned["escalated"] == 1
    assert planned["terminal"] == 0
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT current_rung, status, next_action_owner FROM kx.fetch_queue")
        row = cursor.fetchone()
        assert row is not None
        assert row["current_rung"] == "network_robots_override"
        assert row["status"] == "retry"
        assert row["next_action_owner"] == "machine"


def test_an_override_profile_must_carry_a_real_reason(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    with pytest.raises(Exception, match="an_override_says_why"):
        database.write_host_profile(
            HostProfile(
                host="reddit.com",
                rungs=("network", "network_robots_override"),
                robots_policy="override_recorded",
                rationale="because",
                decided_by="owner",
            )
        )


@pytest.mark.parametrize("error_code", ["timeout", "network_error", "http_503"])
def test_a_transient_failure_says_nothing_about_the_document(error_code: str) -> None:
    # 79 documents in production were filed under "a person must decide" because
    # a request timed out. That is how a gap queue stops being read.
    step = next_step(profile=HostProfile(host="x"), tried=["network"], error_code=error_code)
    assert step.terminal_reason == "transient_exhausted"
    assert step.next_action_owner == "machine"


def test_every_escalation_hint_names_an_error_the_fetcher_can_emit() -> None:
    # Two of the first five rules named codes that do not exist and could never
    # have fired. The vocabulary is the fetcher's, not this module's guess at it.
    source = (Path(__file__).parents[1] / "src" / "radar_kx" / "fetcher.py").read_text(
        encoding="utf-8"
    )
    emitted = set(re.findall(r'"([a-z_0-9]+)"', source)) | {
        f"http_{code}" for code in (401, 403, 404, 429, 500, 503)
    }
    for error_code in (*ESCALATION_HINT, *TERMINAL_BY_ERROR, *TRANSIENT_ERRORS):
        if error_code.startswith("http_"):
            continue
        assert error_code in emitted, f"{error_code} is not a code the fetcher emits"


def test_every_hinted_rung_is_a_real_rung() -> None:
    for rung in ESCALATION_HINT.values():
        assert rung in LADDER


def test_a_rung_no_machine_can_climb_is_a_person_even_without_a_profile() -> None:
    # 80 documents on production whose text only a renderer gets. These rows are
    # the measured need ADR-0005 §11 asks for before the browser unit is built,
    # and they must not be filed as "the host blocked us" - nothing blocked us.
    step = next_step(
        profile=HostProfile(host="x.example"),
        tried=["network"],
        error_code="weak_or_missing_text",
    )
    assert step.terminal_reason == "ladder_exhausted"
    assert step.next_action_owner == "operator"
    assert "browser_render" in step.detail
