"""Building, publishing and rolling back a knowledge release (slice 3.1).

Everything the project has built lives in `kx`, which holds other people's full
text. A reader must never be one bug away from it, so the published slice lives
in `kb` and the service that serves it has SELECT on `kb` and nothing else. That
is what "its own blast radius" (P35) means when it is written as a grant rather
than as an intention: the service can be wrong about a scope and still be unable
to return somebody's article, because the rows are not reachable from its
connection.

Four properties, and where each one lives:

``immutable``     composition and counters are fixed at build time and the table
                  refuses UPDATE and DELETE.
``atomic``        the active pointer is one row, moved by one UPDATE inside one
                  transaction. There is no window in which nothing is published.
``reversible``    the previous release is still in `kb`. Rolling back is the same
                  pointer moving the other way, recorded like any other event.
``reconcilable``  the slice can be compared against `kx` and the difference named,
                  because a release that has quietly drifted from its source is
                  worse than one that is out of date.

What goes into a slice is decided here and the rule is one sentence: **only what
somebody or something has already confirmed.** A published quotation cleared the
five conditions of P19. A statement's evidence is a binding a person confirmed,
never a proposal. An idea cleared the independence gate of P13. A slice that
shipped proposals as evidence would be the one failure this whole design exists
to prevent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from radar_kx.identifiers import sha256_bytes

#: Audiences an element can carry. ADR-0006 §1.4 fixes the first version at the
#: constant `public` and requires the service to check it from day one: a check
#: added later is a check that was missing in between.
AUDIENCES = ("public", "editor")

RELEASE_ACTIONS = ("built", "published", "rolled_back", "superseded")

#: How much of the state hash goes into the identifier.
ID_HASH_CHARS = 16


class ReleaseError(RuntimeError):
    """The release cannot be built, published or rolled back."""


@dataclass(frozen=True, slots=True)
class SliceElement:
    """One thing in the slice, in the form the manifest hashes."""

    kind: str
    element_id: str
    fingerprint: str

    def manifest_line(self) -> str:
        return f"{self.kind}\t{self.element_id}\t{self.fingerprint}"


@dataclass(frozen=True, slots=True)
class ReleaseComposition:
    """What a release contains, and the one value that changes when it changes."""

    elements: tuple[SliceElement, ...]
    wiki_snapshot_id: str | None
    graph_snapshot_id: str | None
    family_decision_high_water: int

    @property
    def state_sha256(self) -> str:
        """Sorted, so the order rows came back in cannot change a release's identity."""
        lines = sorted(element.manifest_line() for element in self.elements)
        lines.append(f"wiki\t{self.wiki_snapshot_id or ''}")
        lines.append(f"graph\t{self.graph_snapshot_id or ''}")
        lines.append(f"families\t{self.family_decision_high_water}")
        return sha256_bytes("\n".join(lines).encode("utf-8"))

    @property
    def release_id(self) -> str:
        return f"kb-{self.state_sha256[:ID_HASH_CHARS]}"

    def count(self, kind: str) -> int:
        return sum(1 for element in self.elements if element.kind == kind)

    def as_json(self) -> dict[str, Any]:
        return {
            "releaseId": self.release_id,
            "stateSha256": self.state_sha256,
            "wikiSnapshotId": self.wiki_snapshot_id,
            "graphSnapshotId": self.graph_snapshot_id,
            "familyDecisionHighWater": self.family_decision_high_water,
            "quotes": self.count("quote"),
            "concepts": self.count("concept"),
            "statements": self.count("statement"),
            "ideas": self.count("idea"),
            "elements": len(self.elements),
        }


def compose(
    *,
    quotes: Sequence[dict[str, Any]],
    concepts: Sequence[dict[str, Any]],
    statements: Sequence[dict[str, Any]],
    ideas: Sequence[dict[str, Any]],
    wiki_snapshot_id: str | None,
    graph_snapshot_id: str | None,
    family_decision_high_water: int,
) -> ReleaseComposition:
    """Turn what was selected into a composition with a stable identity.

    The fingerprint of an element is what a reader would notice changing. For a
    quotation that is the text and its attribution; for a statement it is the
    text and how much confirmed evidence stands under it, because a statement
    that gained evidence is a different thing to publish even though its words
    did not move.
    """
    elements: list[SliceElement] = []
    for row in quotes:
        elements.append(
            SliceElement(
                "quote",
                str(row["quote_id"]),
                sha256_bytes(
                    "\n".join(
                        (
                            str(row["original_text"]),
                            str(row.get("translated_text") or ""),
                            str(row["attribution"]),
                            str(row.get("caveat") or ""),
                        )
                    ).encode("utf-8")
                ),
            )
        )
    for row in concepts:
        elements.append(SliceElement("concept", str(row["concept_id"]), str(row["body_sha256"])))
    for row in statements:
        elements.append(
            SliceElement(
                "statement",
                str(row["statement_id"]),
                sha256_bytes(f"{row['statement']}\n{row['confirmed_evidence']}".encode()),
            )
        )
    for row in ideas:
        elements.append(
            SliceElement(
                "idea",
                str(row["idea_id"]),
                sha256_bytes(
                    f"{row['title']}\n{row['statement']}\n{row['independent_sources']}".encode()
                ),
            )
        )
    return ReleaseComposition(
        elements=tuple(elements),
        wiki_snapshot_id=wiki_snapshot_id,
        graph_snapshot_id=graph_snapshot_id,
        family_decision_high_water=family_decision_high_water,
    )


@dataclass(frozen=True, slots=True)
class ReleaseReconciliation:
    """How the published slice differs from what KX would build today."""

    release_id: str
    active: bool
    missing_from_slice: tuple[str, ...]
    absent_from_source: tuple[str, ...]
    changed: tuple[str, ...]

    @property
    def identical(self) -> bool:
        return not (self.missing_from_slice or self.absent_from_source or self.changed)

    def as_json(self) -> dict[str, Any]:
        return {
            "releaseId": self.release_id,
            "active": self.active,
            "identical": self.identical,
            # A published release is expected to fall behind: it is a snapshot and
            # the store keeps moving. What must never happen is not knowing.
            "missingFromSlice": len(self.missing_from_slice),
            "absentFromSource": len(self.absent_from_source),
            "changed": len(self.changed),
            "examples": {
                "missingFromSlice": list(self.missing_from_slice[:10]),
                "absentFromSource": list(self.absent_from_source[:10]),
                "changed": list(self.changed[:10]),
            },
        }


def reconcile(
    release_id: str,
    *,
    active: bool,
    published: dict[str, str],
    current: dict[str, str],
) -> ReleaseReconciliation:
    """Compare a published slice with what the store would build now.

    Keyed by ``kind:id`` to fingerprint on both sides. Three differences, and they
    mean different things: something the store has and the slice does not is work
    a new release would pick up; something the slice has and the store does not is
    an element that was withdrawn after publication; something whose fingerprint
    moved is text a reader would see change.
    """
    published_keys, current_keys = set(published), set(current)
    return ReleaseReconciliation(
        release_id=release_id,
        active=active,
        missing_from_slice=tuple(sorted(current_keys - published_keys)),
        absent_from_source=tuple(sorted(published_keys - current_keys)),
        changed=tuple(
            sorted(key for key in published_keys & current_keys if published[key] != current[key])
        ),
    )
