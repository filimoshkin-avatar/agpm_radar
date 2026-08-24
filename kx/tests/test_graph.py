"""Slice 2.11: the graph adds no facts, it adds a shape taken at one moment."""

from __future__ import annotations

from typing import Any

import pytest

from radar_kx.graph import (
    AUTHORED_RELATIONS,
    MAX_LABEL_CHARS,
    STRUCTURAL_RELATIONS,
    Graph,
    build,
    dangling,
    label_of,
    node_id,
    unsupported,
)

CONCEPTS = [
    {
        "concept_id": "c1",
        "title": "Two-layer accountability",
        "relative_path": "wiki/responsibility/two-layer.md",
        "layer": "synthesis_page",
        "language": "en",
    }
]
CONCEPT_CLAIMS = [
    {
        "concept_claim_id": "cc1",
        "statement": "An agentic run assigns accountability to one named human owner.",
        "claim_nature": "descriptive",
        "segmentation": "list_item",
        "concept_id": "c1",
    },
    {
        "concept_claim_id": "cc2",
        "statement": "Nothing has ever been bound to this one.",
        "claim_nature": "open_question",
        "segmentation": "list_item",
        "concept_id": "c1",
    },
]
CLAIMS = [
    {
        "claim_id": "k1",
        "state": "proposed",
        "version_id": "v1",
        "char_start": 10,
        "char_end": 60,
        "quote_text": "assigns accountability to one named human owner",
        "document_id": "d1",
        "language": "en",
        "canonical_url": "https://example.com/a",
    }
]
IDEAS = [
    {
        "idea_id": "i1",
        "title": "Ownership is named",
        "state": "proposed",
        "admitted": True,
        "independent_sources": 2,
    }
]


def _graph(**overrides: Any) -> Graph:
    arguments: dict[str, Any] = {
        "concepts": CONCEPTS,
        "concept_claims": CONCEPT_CLAIMS,
        "concept_evidence": [],
        "ideas": IDEAS,
        "idea_evidence": [{"idea_id": "i1", "claim_id": "k1", "stance": "support"}],
        "claims": CLAIMS,
        "families": [
            {
                "document_id": "d1",
                "family_id": "f1",
                "family_key": "example-com",
                "family_kind": "owner",
            }
        ],
    }
    arguments.update(overrides)
    return build(**arguments)


def test_every_assertion_has_a_way_down_to_the_characters() -> None:
    # Priority 2 of the plan, and the reason the graph is worth having: a graph of
    # assertions with no way down to the span is the thing this project exists not
    # to build.
    graph = _graph()
    relations = {(edge.from_node_id, edge.relation, edge.to_node_id) for edge in graph.edges}
    assert (node_id("claim", "k1"), "evidenced_by", node_id("version", "v1")) in relations
    assert (node_id("version", "v1"), "quoted_from", node_id("document", "d1")) in relations
    evidence = next(edge for edge in graph.edges if edge.relation == "evidenced_by")
    assert evidence.attributes == {"charStart": 10, "charEnd": 60}


def test_the_two_edge_vocabularies_are_kept_apart() -> None:
    # An authored edge is a claim somebody made about the world; a structural one
    # describes how the store is wired. Mixing them lets a reader mistake plumbing
    # for an argument.
    assert set(AUTHORED_RELATIONS).isdisjoint(STRUCTURAL_RELATIONS)
    graph = _graph()
    assert graph.as_json()["authoredEdges"] == 0


def test_an_unconfirmed_binding_is_not_drawn() -> None:
    # On a diagram a proposal is indistinguishable from a confirmation, and the
    # whole point of the confirmation is that it is visible.
    proposed = _graph(
        concept_evidence=[
            {
                "concept_claim_id": "cc1",
                "claim_id": "k1",
                "membership_class": "historical",
                "confirmed_at": None,
            }
        ]
    )
    assert proposed.as_json()["authoredEdges"] == 0
    confirmed = _graph(
        concept_evidence=[
            {
                "concept_claim_id": "cc1",
                "claim_id": "k1",
                "membership_class": "historical",
                "confirmed_at": "2026-08-23",
            }
        ]
    )
    assert confirmed.as_json()["authoredEdges"] == 1


def test_an_edge_to_a_node_the_graph_does_not_have_is_dropped() -> None:
    # A graph that points at a node it does not contain is worse than a smaller
    # graph: the renderer draws the edge and the reader follows it nowhere.
    graph = _graph(idea_evidence=[{"idea_id": "i1", "claim_id": "missing", "stance": "support"}])
    assert dangling(graph) == []
    assert not any(edge.relation == "proposed_from" for edge in graph.edges)


def test_the_same_store_projects_to_the_same_snapshot() -> None:
    assert _graph().snapshot_id() == _graph().snapshot_id()


def test_one_more_edge_is_a_different_graph() -> None:
    without = _graph(idea_evidence=[])
    with_edge = _graph()
    assert without.manifest_sha256 != with_edge.manifest_sha256


def test_a_statement_nobody_bound_is_counted_not_hidden() -> None:
    # Not an error: it is exactly what the "statements without evidence" report
    # counts. Reported so the graph does not look complete.
    # Both statements have a page pointing at them; neither has evidence under
    # it, and having a parent is not having evidence.
    assert unsupported(_graph()) == {"concept_claim": 2, "idea": 0} or unsupported(_graph()) == {
        "concept_claim": 2
    }


def test_a_label_is_for_scanning_a_diagram_not_for_reading() -> None:
    long = "word " * 200
    assert len(label_of(long)) <= MAX_LABEL_CHARS
    assert label_of("  two   spaces  ") == "two spaces"


def test_an_unknown_node_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown node kind"):
        node_id("hunch", "x")


def test_every_attribute_the_graph_carries_survives_json() -> None:
    """A snapshot is written as JSON, so an attribute that will not serialise
    stops `build-graph` after it has done all of its work.

    `claim_topics.confidence` is numeric and psycopg hands it back as `Decimal`,
    which `json.dumps` refuses - found on production with the whole projection
    already built.
    """
    import json

    from radar_kx.graph import build

    graph = build(
        concepts=[],
        concept_claims=[],
        concept_evidence=[],
        ideas=[],
        idea_evidence=[],
        claims=[
            {
                "claim_id": "11111111-1111-1111-1111-111111111111",
                "state": "active",
                "version_id": "a" * 64,
                "char_start": 0,
                "char_end": 4,
                "quote_text": "текст",
                "document_id": "b" * 64,
                "language": "ru",
                "canonical_url": "https://example.org/a",
            }
        ],
        families=[],
        topics=[{"topic_key": "t-one", "title": "Тема", "level": 1, "path": "Тема"}],
        placements=[
            {
                "claim_id": "11111111-1111-1111-1111-111111111111",
                "topic_key": "t-one",
                "confidence": 0.75,
            }
        ],
        knowledge_links=[],
    )
    json.dumps(graph.as_json(), ensure_ascii=False)
    for edge in graph.edges:
        json.dumps(edge.attributes, ensure_ascii=False)
    assert any(edge.relation == "about" for edge in graph.edges)
