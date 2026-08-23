"""The graph as a projection taken at one moment (slice 2.11).

Nothing in the graph is new. A concept is a wiki page, an idea is a candidate the
independence gate admitted, a claim is a span in a stored document. What the graph
adds is a shape that can be handed to a reader and to a renderer, and that is why
it is a **snapshot** rather than a set of views: a view answers today's question
with today's data, and a reader following an edge tomorrow would land somewhere
else. A published release points at one snapshot, and "the graph as it was when
this was published" is then a thing that exists.

Priority 1 is concepts, ideas and claims (plan §13.3). Priority 2 is the evidence
trace - claim to version to document - because a graph of assertions with no way
down to the span is the thing this project exists not to build.

Two edge vocabularies are kept apart on purpose. `supports`, `extends`,
`constrains`, `contradicts`, `operationalizes` and `depends-on` are SCHEMA.md's,
carried unchanged (P24), and every one of them is an editorial claim somebody
made. `states`, `evidenced_by`, `quoted_from`, `belongs_to` and `proposed_from`
describe how the store is wired and assert nothing about the world. Mixing them
would let a reader mistake plumbing for an argument.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from radar_kx.identifiers import sha256_bytes

NODE_KINDS = (
    "concept",
    "concept_claim",
    "idea",
    "claim",
    "version",
    "document",
    "source_family",
)

#: Edges somebody authored. Every one is a claim about the world.
AUTHORED_RELATIONS = (
    "supports",
    "extends",
    "constrains",
    "contradicts",
    "operationalizes",
    "depends-on",
)

#: Edges that describe how the store is wired. These assert nothing.
STRUCTURAL_RELATIONS = ("states", "evidenced_by", "quoted_from", "belongs_to", "proposed_from")

RELATIONS = AUTHORED_RELATIONS + STRUCTURAL_RELATIONS

#: How many characters of a label survive into the graph. A node label is for a
#: reader scanning a diagram, not for reading the claim.
MAX_LABEL_CHARS = 120


@dataclass(frozen=True, slots=True)
class Node:
    node_id: str
    node_kind: str
    label: str
    natural_key: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Edge:
    from_node_id: str
    to_node_id: str
    relation: str
    attributes: dict[str, Any] = field(default_factory=dict)


def node_id(kind: str, key: str) -> str:
    """A node's identity is its kind and the row it stands for.

    Prefixed by kind so a claim and the version it sits in cannot collide, and so
    an edge is legible without a lookup.
    """
    if kind not in NODE_KINDS:
        raise ValueError(f"unknown node kind {kind!r}")
    return f"{kind}:{key}"


def label_of(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_LABEL_CHARS:
        return collapsed
    return collapsed[: MAX_LABEL_CHARS - 1] + "…"


@dataclass(frozen=True, slots=True)
class Graph:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]

    @property
    def manifest_sha256(self) -> str:
        """One value over the sorted nodes and edges.

        Sorted, so the order rows happen to come back in cannot change the
        identity of a snapshot; and over the edges as well as the nodes, because
        a graph that gained an edge is a different graph.
        """
        lines = sorted(f"n\t{node.node_id}\t{node.natural_key}" for node in self.nodes)
        lines += sorted(
            f"e\t{edge.from_node_id}\t{edge.relation}\t{edge.to_node_id}" for edge in self.edges
        )
        return sha256_bytes("\n".join(lines).encode("utf-8"))

    def snapshot_id(self) -> str:
        return f"graph-{self.manifest_sha256[:16]}"

    def as_json(self) -> dict[str, Any]:
        return {
            "snapshotId": self.snapshot_id(),
            "manifestSha256": self.manifest_sha256,
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "nodesByKind": {
                kind: sum(1 for node in self.nodes if node.node_kind == kind)
                for kind in NODE_KINDS
                if any(node.node_kind == kind for node in self.nodes)
            },
            "edgesByRelation": {
                relation: sum(1 for edge in self.edges if edge.relation == relation)
                for relation in RELATIONS
                if any(edge.relation == relation for edge in self.edges)
            },
            "authoredEdges": sum(1 for edge in self.edges if edge.relation in AUTHORED_RELATIONS),
        }


def build(
    *,
    concepts: Sequence[dict[str, Any]],
    concept_claims: Sequence[dict[str, Any]],
    concept_evidence: Sequence[dict[str, Any]],
    ideas: Sequence[dict[str, Any]],
    idea_evidence: Sequence[dict[str, Any]],
    claims: Sequence[dict[str, Any]],
    families: Sequence[dict[str, Any]],
) -> Graph:
    """Project the store into nodes and edges.

    Every edge is dropped rather than dangled if either end is missing. A graph
    that points at a node it does not contain is worse than a smaller graph: the
    renderer will draw the edge and the reader will follow it nowhere.
    """
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []

    def add(node: Node) -> str:
        nodes.setdefault(node.node_id, node)
        return node.node_id

    def link(source: str, target: str, relation: str, **attributes: Any) -> None:
        if source not in nodes or target not in nodes or source == target:
            return
        edges.append(Edge(source, target, relation, dict(attributes)))

    for row in concepts:
        add(
            Node(
                node_id=node_id("concept", str(row["concept_id"])),
                node_kind="concept",
                label=label_of(str(row["title"])),
                natural_key=str(row["relative_path"]),
                attributes={"layer": row.get("layer"), "language": row.get("language")},
            )
        )

    for row in concept_claims:
        identifier = add(
            Node(
                node_id=node_id("concept_claim", str(row["concept_claim_id"])),
                node_kind="concept_claim",
                label=label_of(str(row["statement"])),
                natural_key=str(row["concept_claim_id"]),
                attributes={
                    "nature": row.get("claim_nature"),
                    "segmentation": row.get("segmentation"),
                },
            )
        )
        link(node_id("concept", str(row["concept_id"])), identifier, "states")

    for row in claims:
        claim_node = add(
            Node(
                node_id=node_id("claim", str(row["claim_id"])),
                node_kind="claim",
                label=label_of(str(row["quote_text"])),
                natural_key=str(row["claim_id"]),
                attributes={
                    "state": row.get("state"),
                    "charStart": row.get("char_start"),
                    "charEnd": row.get("char_end"),
                },
            )
        )
        version_node = add(
            Node(
                node_id=node_id("version", str(row["version_id"])),
                node_kind="version",
                label=label_of(str(row["canonical_url"])),
                natural_key=str(row["version_id"]),
                attributes={"language": row.get("language")},
            )
        )
        document_node = add(
            Node(
                node_id=node_id("document", str(row["document_id"])),
                node_kind="document",
                label=label_of(str(row["canonical_url"])),
                natural_key=str(row["canonical_url"]),
                attributes={},
            )
        )
        # Priority 2, and the reason the graph is worth having: every assertion
        # has a way down to the characters it rests on.
        link(
            claim_node,
            version_node,
            "evidenced_by",
            charStart=row.get("char_start"),
            charEnd=row.get("char_end"),
        )
        link(version_node, document_node, "quoted_from")

    for row in families:
        family_node = add(
            Node(
                node_id=node_id("source_family", str(row["family_id"])),
                node_kind="source_family",
                label=label_of(str(row["family_key"])),
                natural_key=str(row["family_key"]),
                attributes={"kind": row.get("family_kind")},
            )
        )
        link(node_id("document", str(row["document_id"])), family_node, "belongs_to")

    for row in ideas:
        add(
            Node(
                node_id=node_id("idea", str(row["idea_id"])),
                node_kind="idea",
                label=label_of(str(row["title"])),
                natural_key=str(row["idea_id"]),
                attributes={
                    "state": row.get("state"),
                    "admitted": row.get("admitted"),
                    "independentSources": row.get("independent_sources"),
                },
            )
        )

    for row in idea_evidence:
        link(
            node_id("idea", str(row["idea_id"])),
            node_id("claim", str(row["claim_id"])),
            "proposed_from",
            stance=row.get("stance"),
        )

    for row in concept_evidence:
        # A proposal is drawn only once somebody confirmed it. An unconfirmed
        # binding on a diagram is indistinguishable from a confirmed one, and the
        # whole point of the confirmation is that it is visible.
        if not row.get("confirmed_at"):
            continue
        link(
            node_id("concept_claim", str(row["concept_claim_id"])),
            node_id("claim", str(row["claim_id"])),
            "supports",
            membershipClass=row.get("membership_class"),
        )

    return Graph(nodes=tuple(nodes.values()), edges=tuple(edges))


def dangling(graph: Graph) -> Sequence[Edge]:
    """Edges whose ends are not both in the graph. Should always be empty."""
    known = {node.node_id for node in graph.nodes}
    return [
        edge
        for edge in graph.edges
        if edge.from_node_id not in known or edge.to_node_id not in known
    ]


#: What "connected to evidence" means for each kind that asserts something. A
#: concept claim is connected when somebody confirmed a binding to a stored claim;
#: an idea when the group it came from is drawn under it.
EVIDENCE_RELATIONS = {
    "concept_claim": frozenset(AUTHORED_RELATIONS),
    "idea": frozenset({"proposed_from"}),
}


def unsupported(graph: Graph) -> dict[str, int]:
    """Nodes that assert something with nothing drawn underneath them.

    Not an error: a wiki statement nobody has bound to evidence yet is exactly
    what the "statements without evidence" report counts. Reported so the graph
    says how much of itself is unsupported instead of looking complete.

    The first version of this asked whether a node had *any* edge, which every
    concept claim does - its page points at it. Having a parent is not having
    evidence.
    """
    supported: dict[str, set[str]] = {kind: set() for kind in EVIDENCE_RELATIONS}
    for edge in graph.edges:
        for kind, relations in EVIDENCE_RELATIONS.items():
            if edge.relation in relations and edge.from_node_id.startswith(f"{kind}:"):
                supported[kind].add(edge.from_node_id)
    counts: dict[str, int] = {}
    for node in graph.nodes:
        if node.node_kind not in EVIDENCE_RELATIONS:
            continue
        if node.node_id in supported[node.node_kind]:
            continue
        counts[node.node_kind] = counts.get(node.node_kind, 0) + 1
    return counts
