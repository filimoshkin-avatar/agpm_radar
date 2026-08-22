"""Choosing the vertical-slice sample, and finding the duplicates inside it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from radar_kx.vertical_slice import (
    Candidate,
    find_duplicate_groups,
    load_candidates,
    requirements,
    select,
)

PROBES = (
    Path(__file__).resolve().parents[1] / "data" / "eval" / "vertical-slice-probes-2026-08-22.json"
)


def _candidate(document_id: str, **overrides: Any) -> Candidate:
    defaults: dict[str, Any] = {
        "document_id": document_id,
        "version_id": f"v-{document_id}",
        "text_sha256": f"hash-{document_id}",
        "canonical_url": f"https://{document_id}.example/a",
        "host": f"{document_id}.example",
        "title": document_id,
        "language": "en",
        "quality": "trafilatura",
        "source_kind": "network",
        "chars": 10_000,
        "number_runs": 20,
        "chunks": 3,
        "selections": 1,
        "perimeters": "mid",
        "key_material": False,
        "access_method": "http_default",
        "archive_used": False,
        "manual_review_required": False,
        "window_signature": tuple(f"{document_id}-{index}" for index in range(16)),
    }
    defaults.update(overrides)
    return Candidate(**defaults)


def test_documents_with_the_same_text_hash_are_one_group() -> None:
    groups = find_duplicate_groups(
        [
            _candidate("a", text_sha256="same"),
            _candidate("b", text_sha256="same"),
            _candidate("c"),
        ]
    )
    assert len(groups) == 1
    assert groups[0].kind == "exact"
    assert [item.document_id for item in groups[0].members] == ["a", "b"]


def test_a_reprint_is_republication_somewhere_else() -> None:
    # Two pages on one site carrying one text are a duplicate. A reprint is the same
    # text at a different outlet, and only that exercises source independence.
    same_site = find_duplicate_groups(
        [
            _candidate("a", text_sha256="same", host="one.example"),
            _candidate("b", text_sha256="same", host="one.example"),
        ]
    )
    assert same_site[0].cross_host is False
    elsewhere = find_duplicate_groups(
        [
            _candidate("a", text_sha256="same", host="one.example"),
            _candidate("b", text_sha256="same", host="two.example"),
        ]
    )
    assert elsewhere[0].cross_host is True


def test_a_large_group_of_short_identical_texts_is_boilerplate() -> None:
    # Nine perimeter documents share one 215-character page footer. It is complete
    # full text by the schema and evidence for nothing.
    footer = [
        _candidate(f"y{index}", text_sha256="footer", host="youtube.com", chars=215)
        for index in range(9)
    ]
    groups = find_duplicate_groups(footer)
    assert groups[0].boilerplate is True
    article = find_duplicate_groups(
        [_candidate(f"a{index}", text_sha256="same", chars=7_000) for index in range(3)]
    )
    assert article[0].boilerplate is False


def test_near_duplicates_are_found_by_window_signature_when_hashes_differ() -> None:
    shared = tuple(f"w{index}" for index in range(16))
    groups = find_duplicate_groups(
        [
            _candidate("a", window_signature=shared),
            _candidate("b", window_signature=(*shared[:4], *(f"z{i}" for i in range(12)))),
        ]
    )
    assert len(groups) == 1
    assert groups[0].kind == "near"


def test_selection_records_why_each_document_is_there() -> None:
    population = [
        _candidate("ru1", language="ru", perimeters="near", chars=50_000),
        _candidate("ru2", language="ru", perimeters="mid"),
        _candidate("short", chars=800, perimeters="far"),
        _candidate("archive", access_method="web_archive", archive_used=True),
        _candidate("operator", access_method="operator_file"),
        *(_candidate(f"filler{index}", perimeters="far") for index in range(10)),
    ]
    selection = select(population, size=8)
    result = selection.as_json()
    # Coverage outranks the target size: the named requirements need more than 8.
    assert result["size"] >= 8
    reasons = {item["documentId"]: item["selectedFor"] for item in result["documents"]}
    assert "web_archive" in reasons["archive"]
    assert "operator_artifact" in reasons["operator"]
    assert "short_document" in reasons["short"]
    assert all(reasons.values()), "every document says why it is in the sample"


def test_a_requirement_that_cannot_be_met_is_reported_not_dropped() -> None:
    # There is no cross-host reprint in the perimeter, and the slice must say so
    # rather than appear to cover a class it does not contain.
    selection = select([_candidate(f"d{index}") for index in range(12)], size=6)
    gaps = {item["requirement"]: item for item in selection.as_json()["requirementGaps"]}
    assert "cross_host_reprint" in gaps
    assert gaps["cross_host_reprint"]["available"] == 0
    assert gaps["cross_host_reprint"]["why"]


def test_boilerplate_never_fills_the_short_document_slot() -> None:
    # A 215-character footer is the shortest thing in the corpus and would win the
    # short-document slot, making that slot measure nothing.
    population = [
        *(
            _candidate(f"y{index}", text_sha256="footer", host="youtube.com", chars=215)
            for index in range(9)
        ),
        _candidate("genuinely-short", chars=1_200),
        *(_candidate(f"d{index}", chars=9_000) for index in range(8)),
    ]
    selection = select(population, size=12)
    reasons = {item["documentId"]: item["selectedFor"] for item in selection.as_json()["documents"]}
    assert "short_document" in reasons["genuinely-short"]
    chosen_footers = [key for key in reasons if key.startswith("y")]
    # Exactly one footer document is in, and only as the negative case.
    assert len(chosen_footers) == 1
    assert reasons[chosen_footers[0]] == ["boilerplate_negative"]


def test_selection_is_deterministic() -> None:
    population = [_candidate(f"d{index}", chars=1_000 * (index + 1)) for index in range(20)]
    first = select(population, size=10).as_json()
    second = select(list(reversed(population)), size=10).as_json()
    assert [item["documentId"] for item in first["documents"]] == [
        item["documentId"] for item in second["documents"]
    ]


def test_every_requirement_explains_itself() -> None:
    for requirement in requirements(frozenset(), frozenset(), frozenset()):
        assert requirement.why
        assert requirement.minimum >= 1


def test_the_shipped_selection_matches_the_shipped_probe_set() -> None:
    # The probe set is generated from the selection; if they drift, the measurement
    # is against documents the slice does not contain.
    payload = json.loads(PROBES.read_text(encoding="utf-8"))
    assert payload["questions"]
    assert payload["skipped"]["documents"], "the boilerplate document is named, not silently gone"
    for question in payload["questions"]:
        assert question["kind"] == "probe"
        assert len(question["expectedDocuments"]) == 1
        assert len(question["question"]) >= 40


def test_load_candidates_reads_the_extract_shape() -> None:
    payload = {
        "documents": [
            {
                "documentId": "a" * 64,
                "versionId": "b" * 64,
                "textSha256": "c" * 64,
                "canonicalUrl": "https://example.com/a",
                "host": "example.com",
                "title": "A",
                "language": "en",
                "quality": "trafilatura",
                "sourceKind": "network",
                "chars": 4_000,
                "numberRuns": 60,
                "chunks": 2,
                "selections": 2,
                "perimeters": "mid,near",
                "keyMaterial": True,
                "accessMethod": None,
                "archiveUsed": False,
                "manualReviewRequired": False,
                "windowSignature": ["w1", "w2"],
            }
        ]
    }
    (candidate,) = load_candidates(payload)
    assert candidate.numeric_density == 15.0
    assert candidate.access_method is None
    assert candidate.window_signature == ("w1", "w2")
