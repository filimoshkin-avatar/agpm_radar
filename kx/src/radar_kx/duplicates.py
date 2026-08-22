"""Content duplicate clusters: the same text counted once (ADR-0007 §2, §10).

Two rules, and the difference between them is recorded rather than smoothed over:

* **an identical canonical text hash is certain.** Nothing was inferred; the two
  documents hold the same characters. Slice 1.1 already found 734 complete
  versions corpus-wide sharing a text with another, so this is not a corner case.
* **shingle overlap is probable**, measured two ways because one was not enough.
  Jaccard answers "are these the same document". Containment - the shared
  shingles as a share of the *shorter* text - answers "is one of these inside the
  other", which is the shape syndication actually takes: an outlet wraps a press
  release in its own chrome, and Jaccard punishes the reprint for that chrome.
  Measured on the perimeter the difference decides the slice: at Jaccard 0.80
  nothing was found, and containment found one GlobeNewswire release in two
  outlets, a Deloitte release carried almost verbatim by Yahoo Finance, and the
  AgPM Manifesto announced by two unrelated Russian domains. The threshold, the
  width and the measure are all stored with the cluster, because thresholds
  misclassify and a later review has to be able to tell which rule produced which
  cluster instead of guessing.

A third signal - two documents citing the same primary source - is a hint and
never forms a cluster on its own (ADR-0007 §10). Migration 005 carries that rule
in its types: ``formation_method`` has no value for it.

Nothing here confirms anything. A proposed cluster does not collapse a count
until a person confirms it; the counting function in migration 005 only looks at
confirmed clusters.

Cost. The shingle pass is exact pairwise Jaccard, which is O(n²): fine for the
275-document perimeter, not fine for 8313. There is no sampling and no silent
cap - the caller passes the set to compare and the report says how many were
compared, so a future need for LSH shows up as a slow run rather than as a
quietly incomplete answer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: Words per shingle. Five is small enough to survive an editor's cuts and large
#: enough that ordinary phrases do not collide.
DEFAULT_SHINGLE_WIDTH = 5

#: Jaccard overlap above which two texts are proposed as one cluster. Kept at the
#: conventional value; on the perimeter it fires on nothing that containment does
#: not already catch, and it is the right measure for two copies of one document.
DEFAULT_SHINGLE_THRESHOLD = 0.80

#: Containment above which the shorter text is proposed as carried by the longer.
#: Measured, not assumed: the four pairs the perimeter contains score 1.000, 1.000,
#: 0.969 and 0.773, and nothing else reaches 0.5. Set below the lowest of the four
#: rather than above it, because all four are real. This is a number chosen against
#: 275 documents and it is stored with every cluster it forms, so a later review can
#: tell which clusters it produced.
DEFAULT_CONTAINMENT_THRESHOLD = 0.75

#: Below this many shingles a document is too short to judge by overlap: two
#: 40-word notices about the same event look identical by Jaccard and are not
#: reprints of each other.
MIN_SHINGLES = 40

_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class PairEvidence:
    """Why one pair of documents is in a cluster, with both measures kept."""

    left: str
    right: str
    #: The value of the measure named by ``measure`` - what the rule fired on.
    similarity: float
    measure: str
    jaccard: float
    containment: float

    def as_json(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "measure": self.measure,
            "similarity": round(self.similarity, 4),
            "jaccard": round(self.jaccard, 4),
            "containment": round(self.containment, 4),
        }


@dataclass(frozen=True, slots=True)
class DocumentText:
    document_id: str
    canonical_url: str
    text_sha256: str
    text: str


@dataclass(frozen=True, slots=True)
class DuplicateProposal:
    """One proposed cluster with the evidence that formed it."""

    cluster_kind: str
    formation_method: str
    document_ids: tuple[str, ...]
    #: One entry per pair that put the group together: the two documents, the
    #: value the rule fired on, and both measures for a later reader.
    pairs: tuple[PairEvidence, ...]
    shingle_threshold: float | None = None
    shingle_width: int | None = None
    shingle_measure: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "clusterKind": self.cluster_kind,
            "formationMethod": self.formation_method,
            "documentIds": list(self.document_ids),
            "shingleThreshold": self.shingle_threshold,
            "shingleWidth": self.shingle_width,
            "shingleMeasure": self.shingle_measure,
            "pairs": [pair.as_json() for pair in self.pairs],
        }


def shingles(text: str, width: int = DEFAULT_SHINGLE_WIDTH) -> frozenset[str]:
    """Word shingles, lowercased and stripped of punctuation.

    Case and punctuation are dropped because a reprint that changed a comma is
    still a reprint, and keeping them would make the threshold measure typography
    rather than content.
    """
    words = [word.lower() for word in _WORD.findall(text)]
    if len(words) < width:
        return frozenset()
    return frozenset(
        " ".join(words[index : index + width]) for index in range(len(words) - width + 1)
    )


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def containment(left: frozenset[str], right: frozenset[str]) -> float:
    """Shared shingles as a share of the shorter text.

    Asymmetric in meaning and symmetric in value: it says "one of these is inside
    the other" without saying which, which is all a cluster needs.
    """
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _components(nodes: Sequence[str], edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Union-find, so a chain of pairwise matches becomes one cluster."""
    parent = {node: node for node in nodes}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in edges:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root
    groups: dict[str, list[str]] = {}
    for node in nodes:
        groups.setdefault(find(node), []).append(node)
    return [sorted(members) for members in groups.values() if len(members) > 1]


def find_hash_clusters(documents: Sequence[DocumentText]) -> tuple[DuplicateProposal, ...]:
    """Group documents whose canonical text is byte-identical."""
    by_hash: dict[str, list[str]] = {}
    for document in documents:
        by_hash.setdefault(document.text_sha256, []).append(document.document_id)
    proposals: list[DuplicateProposal] = []
    for _, members in sorted(by_hash.items()):
        if len(members) < 2:
            continue
        ordered = tuple(sorted(members))
        proposals.append(
            DuplicateProposal(
                cluster_kind="reprint",
                formation_method="canonical_text_hash",
                document_ids=ordered,
                pairs=tuple(
                    PairEvidence(
                        left=ordered[0],
                        right=other,
                        similarity=1.0,
                        measure="canonical_text_hash",
                        jaccard=1.0,
                        containment=1.0,
                    )
                    for other in ordered[1:]
                ),
            )
        )
    return tuple(proposals)


def find_shingle_clusters(
    documents: Sequence[DocumentText],
    *,
    threshold: float = DEFAULT_SHINGLE_THRESHOLD,
    containment_threshold: float = DEFAULT_CONTAINMENT_THRESHOLD,
    width: int = DEFAULT_SHINGLE_WIDTH,
    exclude: frozenset[str] = frozenset(),
) -> tuple[tuple[DuplicateProposal, ...], dict[str, Any]]:
    """Propose clusters by pairwise overlap, and report what was compared.

    A pair joins a cluster if **either** measure clears its threshold. Containment
    is preferred when both do, because when a press release sits inside a longer
    page containment is the number that describes the relationship and Jaccard is
    the number that describes the host's chrome.

    ``exclude`` takes the documents an exact-hash cluster already accounts for:
    running them through the slow path would rediscover the same group with a
    similarity of 1 and record it under a rule that only says "probable".
    """
    if not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1]")
    if not 0 < containment_threshold <= 1:
        raise ValueError("containment_threshold must be in (0, 1]")
    prepared = [
        (document.document_id, shingles(document.text, width))
        for document in documents
        if document.document_id not in exclude
    ]
    comparable = [(identifier, bag) for identifier, bag in prepared if len(bag) >= MIN_SHINGLES]
    stats: dict[str, Any] = {
        "documents": len(documents),
        "excluded": len(documents) - len(prepared),
        "tooShort": len(prepared) - len(comparable),
        "compared": len(comparable),
        "pairs": len(comparable) * (len(comparable) - 1) // 2,
        "jaccardThreshold": threshold,
        "containmentThreshold": containment_threshold,
    }
    matched: dict[tuple[str, str], PairEvidence] = {}
    for index, (left_id, left_bag) in enumerate(comparable):
        for right_id, right_bag in comparable[index + 1 :]:
            overlap = jaccard(left_bag, right_bag)
            inside = containment(left_bag, right_bag)
            if inside >= containment_threshold:
                measure, similarity = "containment", inside
            elif overlap >= threshold:
                measure, similarity = "jaccard", overlap
            else:
                continue
            matched[(left_id, right_id)] = PairEvidence(
                left=left_id,
                right=right_id,
                similarity=similarity,
                measure=measure,
                jaccard=overlap,
                containment=inside,
            )
    groups = _components([identifier for identifier, _ in comparable], matched)
    proposals: list[DuplicateProposal] = []
    for members in sorted(groups):
        inside_group = frozenset(members)
        pairs = tuple(
            evidence
            for (left, right), evidence in sorted(matched.items())
            if left in inside_group and right in inside_group
        )
        # One cluster, one rule: containment if any pair needed it, because that is
        # the weaker claim of the two and the cluster should be labelled with what
        # actually holds it together.
        measure = (
            "containment" if any(pair.measure == "containment" for pair in pairs) else "jaccard"
        )
        proposals.append(
            DuplicateProposal(
                cluster_kind="reprint",
                formation_method="shingle_overlap",
                document_ids=tuple(members),
                pairs=pairs,
                shingle_threshold=containment_threshold if measure == "containment" else threshold,
                shingle_width=width,
                shingle_measure=measure,
            )
        )
    return tuple(proposals), stats
