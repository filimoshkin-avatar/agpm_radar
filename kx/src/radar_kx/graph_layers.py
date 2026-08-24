"""The two layers of graph edges, and the honesty the reader is owed about them.

Authorial relations (`contradicts`, `qualifies`, `related_to`, `supports`) are
claims about knowledge: the linking model proposed them, and by the owner's
decision linking is published without a visa. Structural relations (`about`,
`mentions`) describe how the base is built. A reader must never mistake one for
the other, so every edge carries its layer, and every authorial edge carries its
machine provenance (Links plan §6).
"""

from __future__ import annotations

from typing import Any, Final

GRAPH_SCHEMA_VERSION: Final = "2.0"

#: Which relations are claims about knowledge and which are the base's own
#: structure. Anything unknown stays structural: an unclassified relation must
#: not borrow the authority of a reviewed claim.
GRAPH_EDGE_LAYERS: Final = {
    "contradicts": "authorial",
    "qualifies": "authorial",
    "related_to": "authorial",
    "supports": "authorial",
    "about": "structural",
    "mentions": "structural",
}

GRAPH_EDGE_EXPLANATIONS: Final = {
    "about": "Утверждение отнесено к теме скелета",
    "mentions": "Сущность названа в утверждении",
}

#: Every one of them names both halves of the provenance: which machine step
#: proposed the tie, and that nobody has confirmed it. Saying only the first
#: half lets a model's suggestion read as an established part of the canon,
#: which is the one thing an authorial edge must never do.
_AUTHORIAL_EXPLANATIONS: Final = {
    "contradicts": (
        "Модель определила утверждения как противоречащие; владелец базы это не подтверждал"
    ),
    "qualifies": (
        "Модель определила одно утверждение как уточняющее другое; владелец базы это не подтверждал"
    ),
    "related_to": ("Модель определила утверждения как связанные; владелец базы это не подтверждал"),
    "supports": (
        "Модель определила одно утверждение как поддерживающее другое; "
        "владелец базы это не подтверждал"
    ),
}


def annotate_edge(relation: str) -> dict[str, Any]:
    """The fields an edge carries beside its endpoints.

    An authorial edge says plainly that the machine proposed it and the owner
    has not confirmed it - the graph must never present a model's suggestion as
    an established part of the canon.
    """
    layer = GRAPH_EDGE_LAYERS.get(relation, "structural")
    if layer == "authorial":
        return {
            "layer": layer,
            "method": "model",
            "reviewStatus": "unreviewed",
            "explanation": _AUTHORIAL_EXPLANATIONS.get(
                relation, "Связь предложена машиной; владелец базы её не подтверждал"
            ),
        }
    return {
        "layer": layer,
        "explanation": GRAPH_EDGE_EXPLANATIONS.get(relation, "Структурная связь базы"),
    }


def graph_meta(*, total: int, returned: int, policy: str) -> dict[str, Any]:
    """How much of the neighbourhood the reader is seeing, and why this much.

    A topic can hold a thousand statements and a canvas holds thirty nodes; the
    honest answer is to say both numbers and the selection rule, rather than to
    draw an arbitrary slice and let it read as the whole.
    """
    hidden = max(0, total - returned)
    return {
        "totalNeighborCount": total,
        "returnedNeighborCount": returned,
        "hiddenNodeCount": hidden,
        "truncated": hidden > 0,
        "selectionPolicy": policy,
    }
