"""The two layers of graph edges, and the honesty each owes the reader.

An authorial edge is a model's suggestion published without a visa; a
structural edge is how the base is built. The tests pin the wording and the
meta arithmetic the reader will rely on to know what a drawing is hiding.
"""

from __future__ import annotations

from radar_kx.graph_layers import (
    GRAPH_EDGE_LAYERS,
    GRAPH_SCHEMA_VERSION,
    annotate_edge,
    graph_meta,
)


def test_an_authorial_edge_says_who_proposed_it_and_who_did_not_confirm() -> None:
    annotated = annotate_edge("supports")
    assert annotated["layer"] == "authorial"
    assert annotated["method"] == "model"
    assert annotated["reviewStatus"] == "unreviewed"
    assert "поддерживающее" in annotated["explanation"]
    # The test's own name is the requirement: an explanation that names the
    # machine but not the missing confirmation only tells half of it.
    assert "не подтверждал" in annotated["explanation"]


def test_a_structural_edge_explains_the_base_not_a_claim() -> None:
    annotated = annotate_edge("mentions")
    assert annotated["layer"] == "structural"
    assert "method" not in annotated
    assert "названа" in annotated["explanation"]


def test_an_unknown_relation_is_structural_not_authorial() -> None:
    """Borrowing the authority of a reviewed claim is the one mistake to avoid."""
    assert annotate_edge("states")["layer"] == "structural"


def test_every_drawn_relation_has_a_layer() -> None:
    for relation in ("contradicts", "qualifies", "related_to", "supports", "about", "mentions"):
        assert annotate_edge(relation)["layer"] == GRAPH_EDGE_LAYERS[relation]


def test_meta_counts_what_is_hidden_and_says_how_the_slice_was_chosen() -> None:
    meta = graph_meta(total=1260, returned=30, policy="most-recent-knowledge")
    assert meta["totalNeighborCount"] == 1260
    assert meta["returnedNeighborCount"] == 30
    assert meta["hiddenNodeCount"] == 1230
    assert meta["truncated"] is True
    assert meta["selectionPolicy"] == "most-recent-knowledge"


def test_meta_is_honest_when_nothing_was_cut() -> None:
    meta = graph_meta(total=12, returned=12, policy="all-neighbours")
    assert meta["hiddenNodeCount"] == 0
    assert meta["truncated"] is False


def test_the_schema_version_is_pinned() -> None:
    assert GRAPH_SCHEMA_VERSION == "2.0"
