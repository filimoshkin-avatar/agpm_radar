"""Compare the Project Manager's file stores with KX, and record the difference.

Slice 2.4a, and the obligation owner decision P28 attaches to every coverage
number: the file contour is a working copy, KX is the evidence base, they will
diverge, and the divergence has to be visible at the moment it appears rather
than at the moment it breaks a publication.

Two scopes, because they answer different questions:

``source_fulltext``
    the extracted text the Project Manager keeps beside the registry. A file here
    with no complete version in KX is text we hold and cannot cite.
``discovery_registry``
    ``materials.jsonl``. A row here with no document in KX is something the daily
    pass saw and the evidence base never received.

The file side arrives as an inventory built on the control host, because that is
where the files are; the comparison and the record happen on the host that holds
KX. Only identifiers and counts travel - never the text.

The report is not a gate. It is a number somebody looks at, and it is expected to
be non-zero forever: the two stores have different jobs and different rhythms
(ADR-0008). What would be wrong is not knowing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from radar_kx.identifiers import document_id, sha256_bytes, stable_json_bytes
from radar_kx.url_policy import UnsafeUrlError, canonical_identity_url

SCOPES = ("source_fulltext", "discovery_registry")

#: How many diverging items the report names. The counts are exact; the lists are
#: capped so one bad day cannot write a megabyte into an immutable table.
MAX_LISTED = 200


class ReconciliationError(ValueError):
    """The inventory cannot be compared."""


@dataclass(frozen=True, slots=True)
class FileStoreEntry:
    """One item the file contour holds."""

    canonical_url: str
    #: Characters of text, where the store keeps text. Zero for a registry row.
    text_chars: int
    status: str

    @property
    def document_id(self) -> str | None:
        try:
            return document_id(canonical_identity_url(self.canonical_url))
        except UnsafeUrlError:
            return None


def load_inventory(path: Path) -> tuple[str, tuple[FileStoreEntry, ...], Mapping[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ReconciliationError("inventory must be an object")
    scope = str(payload.get("scope") or "")
    if scope not in SCOPES:
        raise ReconciliationError(f"scope must be one of {list(SCOPES)}, got {scope!r}")
    raw = payload.get("entries")
    if not isinstance(raw, list):
        raise ReconciliationError("inventory has no entries")
    entries = tuple(
        FileStoreEntry(
            canonical_url=str(item["canonicalUrl"]),
            text_chars=int(item.get("textChars") or 0),
            status=str(item.get("status") or "unknown"),
        )
        for item in raw
        if isinstance(item, dict) and item.get("canonicalUrl")
    )
    source = {
        "generatedAt": payload.get("generatedAt"),
        "root": payload.get("root"),
        "entryCount": len(entries),
    }
    return scope, entries, source


@dataclass(frozen=True, slots=True)
class Reconciliation:
    scope: str
    file_store_count: int
    kx_count: int
    only_in_file_store: tuple[str, ...]
    only_in_kx: tuple[str, ...]
    differing: tuple[dict[str, Any], ...]
    unaddressable: tuple[str, ...]
    source: Mapping[str, Any]

    def payload(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "source": dict(self.source),
            "onlyInFileStore": list(self.only_in_file_store[:MAX_LISTED]),
            "onlyInKx": list(self.only_in_kx[:MAX_LISTED]),
            "differing": [dict(item) for item in self.differing[:MAX_LISTED]],
            "unaddressable": list(self.unaddressable[:MAX_LISTED]),
            "listCap": MAX_LISTED,
            "truncated": {
                "onlyInFileStore": max(0, len(self.only_in_file_store) - MAX_LISTED),
                "onlyInKx": max(0, len(self.only_in_kx) - MAX_LISTED),
                "differing": max(0, len(self.differing) - MAX_LISTED),
            },
        }

    def as_json(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "fileStoreCount": self.file_store_count,
            "kxCount": self.kx_count,
            "onlyInFileStore": len(self.only_in_file_store),
            "onlyInKx": len(self.only_in_kx),
            "differing": len(self.differing),
            "unaddressable": len(self.unaddressable),
        }


def compare(
    scope: str,
    entries: Sequence[FileStoreEntry],
    kx: Mapping[str, Mapping[str, Any]],
    *,
    source: Mapping[str, Any],
) -> Reconciliation:
    """Compare the file inventory with what KX holds, keyed by document id.

    ``kx`` maps document id to at least ``hasCompleteVersion`` and ``chars``. For
    ``source_fulltext`` a document that exists without a complete version counts
    as divergent: the file contour has readable text and the evidence base does
    not, which is precisely the case that breaks a citation.
    """
    only_in_files: list[str] = []
    differing: list[dict[str, Any]] = []
    unaddressable: list[str] = []
    seen: set[str] = set()

    for entry in entries:
        identifier = entry.document_id
        if identifier is None:
            unaddressable.append(entry.canonical_url)
            continue
        seen.add(identifier)
        known = kx.get(identifier)
        if known is None:
            only_in_files.append(entry.canonical_url)
            continue
        if scope == "source_fulltext" and not bool(known.get("hasCompleteVersion")):
            differing.append(
                {
                    "canonicalUrl": entry.canonical_url,
                    "why": "the file contour holds text; KX has no complete version",
                    "fileChars": entry.text_chars,
                    "fileStatus": entry.status,
                }
            )

    only_in_kx = sorted(
        str(value.get("canonicalUrl") or key) for key, value in kx.items() if key not in seen
    )
    return Reconciliation(
        scope=scope,
        file_store_count=len(entries),
        kx_count=len(kx),
        only_in_file_store=tuple(sorted(only_in_files)),
        only_in_kx=tuple(only_in_kx),
        differing=tuple(differing),
        unaddressable=tuple(sorted(unaddressable)),
        source=source,
    )


def payload_sha256(payload: Mapping[str, Any]) -> str:
    return sha256_bytes(stable_json_bytes(dict(payload)))
