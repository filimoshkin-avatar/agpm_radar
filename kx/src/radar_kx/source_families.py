"""Source families: who is actually a separate observer (defect D13, ADR-0007).

The radar's perimeter is news, and news propagates by reprint. A rating that
counts "how many sources say this" and an idea gate that wants "two supporting
claims from different sources" both read repetition as corroboration unless
something tells them that twelve articles were one press release.

A family is an **editorial fact, not a computed one** (ADR-0007 §11). This module
therefore proposes and never decides: it groups documents by registrable domain,
writes a batch with the evidence for each grouping, and the owner confirms the
batch. One decision per family, not per document, because the perimeter alone
spans 198 hosts and per-document confirmation would not fit the 15-30 minutes a
day of P15 (ADR-0007 §11a).

What the proposal is worth: a domain grouping finds the easy half - the same
outlet under several hosts - and misses the interesting half, two unrelated
domains with one editorial desk. That is the half a person is for. The machine
says what it saw; it does not claim the grouping is right.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from radar_kx.identifiers import sha256_bytes, stable_json_bytes

FAMILY_KINDS = ("owner", "editorial_desk", "syndication_channel")
DECISION_ACTIONS = ("confirmed", "corrected", "retired")

#: Multi-label public suffixes we actually meet. This is a short list, not the
#: Public Suffix List: adding a dependency to group hosts for a proposal a person
#: confirms anyway would buy accuracy nobody needs. A host whose suffix is not
#: here groups by its last two labels, which is wrong for exactly the cases in
#: this list and right otherwise.
COMPOUND_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "co.jp",
        "or.jp",
        "ne.jp",
        "co.kr",
        "com.au",
        "net.au",
        "org.au",
        "com.br",
        "com.cn",
        "com.hk",
        "com.sg",
        "com.tr",
        "co.in",
        "co.il",
        "co.nz",
        "com.mx",
        "com.ar",
        "co.za",
    }
)

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class FamilyBatchError(ValueError):
    """The batch cannot be read or applied."""


def registrable_domain(host: str) -> str:
    """The part of a host that identifies who runs it, as well as we can tell."""
    labels = host.lower().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in COMPOUND_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def family_key(domain: str) -> str:
    slug = _SLUG_STRIP.sub("-", domain.lower()).strip("-")
    return slug[:80] or "unnamed"


@dataclass(frozen=True, slots=True)
class DocumentHost:
    document_id: str
    canonical_url: str

    @property
    def host(self) -> str:
        return (urlsplit(self.canonical_url).hostname or "").lower()


@dataclass(frozen=True, slots=True)
class FamilyProposal:
    """One proposed grouping, with what the machine saw to propose it."""

    family_key: str
    display_name: str
    family_kind: str
    domain: str
    hosts: tuple[str, ...]
    document_ids: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "familyKey": self.family_key,
            "displayName": self.display_name,
            "familyKind": self.family_kind,
            "evidence": {
                "registrableDomain": self.domain,
                "hosts": list(self.hosts),
                "documentCount": len(self.document_ids),
            },
            "documentIds": list(self.document_ids),
        }


def propose_families(documents: Iterable[DocumentHost]) -> tuple[FamilyProposal, ...]:
    """Group documents by registrable domain. Documents with no host are skipped.

    Every group is proposed, including the ones with a single document: a family
    of one is still the difference between "this source" and "unknown", and
    unknown never satisfies a two-independent-sources requirement (ADR-0007 §12).
    """
    grouped: dict[str, dict[str, Any]] = {}
    for document in documents:
        if not document.host:
            continue
        domain = registrable_domain(document.host)
        entry = grouped.setdefault(domain, {"hosts": set(), "documents": set()})
        entry["hosts"].add(document.host)
        entry["documents"].add(document.document_id)
    return tuple(
        FamilyProposal(
            family_key=family_key(domain),
            display_name=domain,
            family_kind="owner",
            domain=domain,
            hosts=tuple(sorted(entry["hosts"])),
            document_ids=tuple(sorted(entry["documents"])),
        )
        for domain, entry in sorted(grouped.items())
    )


def batch_payload(proposals: Sequence[FamilyProposal], *, scope: str) -> dict[str, Any]:
    return {
        "schema": "radar-kx-source-family-batch/v1",
        "scope": scope,
        "note": (
            "Grouped by registrable domain. Confirm, correct or drop each family;"
            " a correction is a new decision, never an edit of an old one."
        ),
        "families": [proposal.as_json() for proposal in proposals],
    }


@dataclass(frozen=True, slots=True)
class FamilyDecision:
    """What the owner decided about one proposed family."""

    family_key: str
    display_name: str
    family_kind: str
    action: str
    rationale: str
    document_ids: tuple[str, ...]

    @property
    def members_sha256(self) -> str:
        return sha256_bytes(stable_json_bytes({"members": sorted(self.document_ids)}))


def load_family_batch(path: Path) -> tuple[str, tuple[FamilyDecision, ...]]:
    """Read a confirmed batch. Anything ambiguous is refused, never guessed."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FamilyBatchError(f"batch is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise FamilyBatchError("batch must be an object")
    decided_by = str(payload.get("decidedBy") or "").strip()
    if not decided_by:
        raise FamilyBatchError("batch must name who decided it")
    raw = payload.get("families")
    if not isinstance(raw, list) or not raw:
        raise FamilyBatchError("batch has no families")
    decisions: list[FamilyDecision] = []
    seen: set[str] = set()
    for item in raw:
        decisions.append(_decision(item, seen))
    return decided_by, tuple(decisions)


def _decision(item: Mapping[str, Any] | Any, seen: set[str]) -> FamilyDecision:
    if not isinstance(item, dict):
        raise FamilyBatchError("every family must be an object")
    key = str(item.get("familyKey") or "").strip()
    if not key:
        raise FamilyBatchError("a family has no familyKey")
    if key in seen:
        raise FamilyBatchError(f"{key} appears twice in one batch")
    seen.add(key)
    action = str(item.get("action") or "").strip()
    if action not in DECISION_ACTIONS:
        raise FamilyBatchError(f"{key}: action must be one of {list(DECISION_ACTIONS)}")
    kind = str(item.get("familyKind") or "owner").strip()
    if kind not in FAMILY_KINDS:
        raise FamilyBatchError(f"{key}: familyKind must be one of {list(FAMILY_KINDS)}")
    rationale = str(item.get("rationale") or "").strip()
    if not rationale:
        # A grouping nobody can explain later is a grouping nobody can review.
        raise FamilyBatchError(f"{key}: a decision must say why")
    documents = item.get("documentIds")
    if not isinstance(documents, list):
        raise FamilyBatchError(f"{key}: documentIds must be a list")
    document_ids = tuple(sorted({str(value) for value in documents}))
    if action != "retired" and not document_ids:
        raise FamilyBatchError(f"{key}: a family that is not retired needs members")
    return FamilyDecision(
        family_key=key,
        display_name=str(item.get("displayName") or key),
        family_kind=kind,
        action=action,
        rationale=rationale,
        document_ids=document_ids,
    )
