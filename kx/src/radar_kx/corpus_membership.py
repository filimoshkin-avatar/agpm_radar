"""Corpus-membership reconciliation across every store that counts Radar materials.

Radar counts its materials in five places with four different units, and until every
transition between them is explained by a query rather than by prose, no coverage metric
has a trustworthy denominator. This module computes that explanation.

The layers, in the order a material passes through them:

``discovery``
    ``knowledge/agpm-radar/data/materials.jsonl`` - the Project Manager's append-mostly
    registry of everything the daily research pass has ever seen. Its own 16-hex id space.
``legacy``
    ``data/db/radar.sqlite`` ``materials`` - the editorial base. One row per
    ``(canonical_url, radar_issue_date)`` pair, so a URL carried into a second issue is
    two rows.
``v2_release``
    the active Radar V2 content release. ``materials`` is a superset of what was ever
    selected; ``issue_materials`` is the selection.
``kx``
    ``kx.issue_perimeter_members`` -> ``kx.documents``. The unit is a document keyed by
    ``sha256(normalize_url(canonical_url))``, so two selections of one URL are one document.
``fulltext``
    ``knowledge/agpm-radar/data/source-fulltext/`` - the Project Manager's working copy of
    extracted text, keyed by ``sha256(canonical_url)[:24]``.

Nothing here touches a network or a production database. The KX layer arrives as the JSON
produced by ``scripts/corpus_membership_kx_extract.sql``, so a report can be reproduced
off-host from a stored extract.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from radar_kx.identifiers import document_id
from radar_kx.url_policy import UnsafeUrlError, normalize_url

JsonObject = dict[str, Any]

#: Radar V2 refuses a material whose resolved publication date falls outside this many days
#: before the issue date. Mirrors ``tools/build_stage14_daily.py``; kept in sync by
#: ``tests/test_corpus_membership.py``.
V2_PUBLICATION_WINDOW_DAYS = 30

#: Namespace prefixes ``packages/legacy_bridge/importer.py`` hashes into a V2 material id.
#: A V2 material is attributed to whichever of these reproduces its id.
_V2_MATERIAL_ORIGINS = (
    ("legacy_material", "{key}"),
    ("legacy_source_metadata", "source-metadata:{key}"),
)


def v2_material_id(legacy_key: str) -> str:
    """Reproduce ``packages/legacy_bridge/importer.py`` ``deterministic_id('material', ...)``."""
    digest = hashlib.sha256(f"radar-v2:material:{legacy_key}".encode()).hexdigest()[:24]
    return f"mat_{digest}"


def fulltext_cache_key(canonical_url: str) -> str:
    """Reproduce the file name the Project Manager gives an extracted-text record."""
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class LegacyMaterial:
    legacy_id: str
    canonical_url: str
    url: str
    issue_date: str
    title: str
    published_at: str | None
    publication_date_status: str


@dataclass(frozen=True, slots=True)
class SelectedMaterial:
    material_id: str
    issue_date: str
    canonical_url: str
    title: str


@dataclass(frozen=True, slots=True)
class V2Release:
    """Everything the reconciliation needs from a sealed Radar V2 content release."""

    materials: tuple[str, ...]
    selected: tuple[SelectedMaterial, ...]
    deferred_materials: frozenset[str]


@dataclass(frozen=True, slots=True)
class DiscoveryRecord:
    discovery_id: str
    canonical_url: str
    perimeter: str | None


@dataclass(frozen=True, slots=True)
class FulltextRecord:
    cache_key: str
    canonical_url: str
    status: str
    text_chars: int


@dataclass(frozen=True, slots=True)
class Check:
    """One arithmetic claim the contract makes about the corpus."""

    name: str
    expected: int
    actual: int
    detail: str

    @property
    def ok(self) -> bool:
        return self.expected == self.actual

    def as_json(self) -> JsonObject:
        return {
            "name": self.name,
            "expected": self.expected,
            "actual": self.actual,
            "ok": self.ok,
            "detail": self.detail,
        }


@dataclass(slots=True)
class _Checks:
    items: list[Check] = field(default_factory=list)

    def add(self, name: str, *, expected: int, actual: int, detail: str) -> None:
        self.items.append(Check(name=name, expected=expected, actual=actual, detail=detail))


def _rows(connection: sqlite3.Connection, query: str) -> Iterator[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    yield from connection.execute(query)


def _open_read_only(path: Path, *, immutable: bool) -> sqlite3.Connection:
    flag = "immutable=1" if immutable else "mode=ro"
    connection = sqlite3.connect(f"file:{path}?{flag}", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_legacy(path: Path) -> tuple[LegacyMaterial, ...]:
    """Read the Legacy editorial base. Read-only; the daily pipeline keeps writing to it."""
    connection = _open_read_only(path, immutable=False)
    try:
        return tuple(
            LegacyMaterial(
                legacy_id=str(row["id"]),
                canonical_url=str(row["canonical_url"] or row["url"]),
                url=str(row["url"]),
                issue_date=str(row["radar_issue_date"]),
                title=str(row["title"]),
                published_at=(str(row["published_at"]) if row["published_at"] else None),
                publication_date_status=str(row["publication_date_status"] or "unresolved"),
            )
            for row in _rows(
                connection,
                """
                SELECT id, title, url, canonical_url, radar_issue_date,
                       published_at, publication_date_status
                FROM materials
                ORDER BY radar_issue_date, id
                """,
            )
        )
    finally:
        connection.close()


def load_legacy_source_metadata_urls(path: Path) -> tuple[str, ...]:
    """Read the URLs of the Legacy metadata-only rows.

    The V2 bootstrap turns a ``source_metadata`` row that matches no material into a
    material of its own, keyed by the row's URL. Without these the release carries rows
    the reconciliation cannot attribute.
    """
    connection = _open_read_only(path, immutable=False)
    try:
        return tuple(
            str(row["url"]).strip()
            for row in _rows(connection, "SELECT url FROM source_metadata ORDER BY url")
            if str(row["url"]).strip()
        )
    finally:
        connection.close()


def load_v2_release(path: Path) -> V2Release:
    """Read a sealed Radar V2 content release: every material, and the issue selection."""
    connection = _open_read_only(path, immutable=True)
    try:
        materials = tuple(
            str(row["material_id"])
            for row in _rows(connection, "SELECT material_id FROM materials ORDER BY material_id")
        )
        selected = tuple(
            SelectedMaterial(
                material_id=str(row["material_id"]),
                issue_date=str(row["issue_date"]),
                canonical_url=str(row["canonical_url"] or row["url"]),
                title=str(row["title"]),
            )
            for row in _rows(
                connection,
                """
                SELECT issue_materials.material_id,
                       issues.issue_date,
                       materials.canonical_url,
                       materials.url,
                       materials.title
                FROM issue_materials
                JOIN issues USING (issue_id)
                JOIN materials USING (material_id)
                ORDER BY issues.issue_date, issue_materials.sort_order
                """,
            )
        )
        deferred = frozenset(
            str(row["material_id"])
            for row in _rows(
                connection,
                "SELECT material_id FROM editorial_queue WHERE state = 'deferred'",
            )
        )
        return V2Release(materials=materials, selected=selected, deferred_materials=deferred)
    finally:
        connection.close()


def active_release_database(content_root: Path) -> Path:
    """Resolve the release database the V2 API is currently serving."""
    active = json.loads((content_root / "active.json").read_text(encoding="utf-8"))
    return (content_root / str(active["database"])).resolve()


def load_discovery(path: Path) -> tuple[DiscoveryRecord, ...]:
    """Read the Project Manager's discovery registry."""
    records: list[DiscoveryRecord] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"discovery registry line is not an object: {line[:80]!r}")
            item: JsonObject = value
            url = item.get("canonical_url") or item.get("url")
            records.append(
                DiscoveryRecord(
                    discovery_id=str(item.get("id") or ""),
                    canonical_url=str(url or ""),
                    perimeter=(str(item["perimeter"]) if item.get("perimeter") else None),
                )
            )
    return tuple(records)


def load_fulltext(directory: Path) -> tuple[FulltextRecord, ...]:
    """Read the Project Manager's extracted-text working copy."""
    records: list[FulltextRecord] = []
    for entry in sorted(directory.glob("*.json")):
        value = json.loads(entry.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"full-text record is not an object: {entry}")
        item: JsonObject = value
        text = item.get("text")
        records.append(
            FulltextRecord(
                cache_key=entry.stem,
                canonical_url=str(item.get("canonical_url") or item.get("url") or ""),
                status=str(item.get("status") or "unknown"),
                text_chars=len(text) if isinstance(text, str) else 0,
            )
        )
    return tuple(records)


def within_v2_publication_window(material: LegacyMaterial) -> bool:
    """Apply the V2 daily eligibility rule to a Legacy row.

    Mirrors ``tools/build_stage14_daily.py``: a material is eligible when its date is
    honestly unknown, or when a resolved date lands inside the window that ends on the
    issue day. Anything else - a resolved status with no date, a date with no status, a
    date older than the window, a future date - is refused.
    """
    status = material.publication_date_status
    published = material.published_at
    if status == "unresolved" and published is None:
        return True
    if status == "unresolved" or published is None:
        return False
    issue_day = date.fromisoformat(material.issue_date)
    earliest = issue_day - timedelta(days=V2_PUBLICATION_WINDOW_DAYS)
    return earliest <= date.fromisoformat(published[:10]) <= issue_day


def _normalized(url: str) -> tuple[str, str | None]:
    """Return ``(normalized_url, error)``; KX refuses what it cannot normalize."""
    try:
        return normalize_url(url), None
    except UnsafeUrlError as exc:
        return url, str(exc)


def _duplicates(values: Iterable[str]) -> dict[str, int]:
    return {value: count for value, count in sorted(Counter(values).items()) if count > 1}


def _attribution_index(legacy_keys: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """Precompute material id -> origin for every key the V2 bootstrap could have hashed."""
    index: dict[str, str] = {}
    for origin, template in _V2_MATERIAL_ORIGINS:
        for key in legacy_keys.get(origin, ()):
            index.setdefault(v2_material_id(template.format(key=key)), origin)
    return index


def _legacy_layer(legacy: Sequence[LegacyMaterial], checks: _Checks) -> JsonObject:
    canonical = [item.canonical_url for item in legacy]
    duplicates = _duplicates(canonical)
    carried = sum(count - 1 for count in duplicates.values())
    checks.add(
        "legacy.rows_equal_distinct_urls_plus_repeat_carries",
        expected=len(legacy),
        actual=len(set(canonical)) + carried,
        detail=(
            "Legacy stores one row per (canonical_url, radar_issue_date); the surplus over "
            "distinct URLs is exactly the URLs carried into a second issue."
        ),
    )
    checks.add(
        "legacy.ids_are_unique",
        expected=len(legacy),
        actual=len({item.legacy_id for item in legacy}),
        detail="Legacy material ids must be unique or the V2 id derivation collides.",
    )
    return {
        "unit": "(canonical_url, radar_issue_date) row",
        "sourceOfTruth": "data/db/radar.sqlite materials",
        "rows": len(legacy),
        "distinctCanonicalUrls": len(set(canonical)),
        "distinctIds": len({item.legacy_id for item in legacy}),
        "issueDates": len({item.issue_date for item in legacy}),
        "repeatCarriedUrls": [
            {
                "canonicalUrl": url,
                "issueDates": sorted(
                    item.issue_date for item in legacy if item.canonical_url == url
                ),
            }
            for url in duplicates
        ],
    }


def _v2_layer(
    legacy: Sequence[LegacyMaterial],
    release: V2Release,
    legacy_metadata_urls: Sequence[str],
    checks: _Checks,
) -> JsonObject:
    index = _attribution_index(
        {
            "legacy_material": [item.legacy_id for item in legacy],
            "legacy_source_metadata": legacy_metadata_urls,
        }
    )
    attribution: Counter[str] = Counter()
    unattributed: list[str] = []
    for material_id in release.materials:
        origin = index.get(material_id)
        if origin is None and material_id in release.deferred_materials:
            # The deferred queue keys on the discovery id inside a JSONL snapshot that the
            # Project Manager rewrites in place, so the key itself is often already gone.
            # The release's own queue row is the durable evidence of that path.
            origin = "legacy_deferred_queue"
        if origin is None:
            origin = "unattributed"
            unattributed.append(material_id)
        attribution[origin] += 1
    checks.add(
        "v2.materials_are_fully_attributed",
        expected=len(release.materials),
        actual=len(release.materials) - len(unattributed),
        detail=(
            "Every material in the release must be reproducible from a Legacy id, a Legacy "
            "source_metadata URL, or a deferred-queue row. An unattributed material means the "
            "release carries a row no current input explains."
        ),
    )
    selected = release.selected
    canonical = [item.canonical_url for item in selected]
    duplicates = _duplicates(canonical)
    return {
        "unit": "issue_materials row (issue x material)",
        "sourceOfTruth": "active Radar V2 content release",
        "materials": len(release.materials),
        "materialsByOrigin": dict(sorted(attribution.items())),
        "unattributedMaterials": unattributed,
        "selectionRows": len(selected),
        "selectionDistinctMaterials": len({item.material_id for item in selected}),
        "selectionDistinctCanonicalUrls": len(set(canonical)),
        "selectionRepeatCarriedUrls": [
            {
                "canonicalUrl": url,
                "issueDates": sorted(
                    item.issue_date for item in selected if item.canonical_url == url
                ),
            }
            for url in duplicates
        ],
    }


def _legacy_to_v2_transition(
    legacy: Sequence[LegacyMaterial],
    selected: Sequence[SelectedMaterial],
    checks: _Checks,
) -> JsonObject:
    selected_keys = {(chosen.canonical_url, chosen.issue_date) for chosen in selected}
    earliest_selection: dict[str, str] = {}
    for chosen in selected:
        current = earliest_selection.get(chosen.canonical_url)
        if current is None or chosen.issue_date < current:
            earliest_selection[chosen.canonical_url] = chosen.issue_date
    outside_window: list[JsonObject] = []
    historical_duplicates: list[JsonObject] = []
    unexplained: list[JsonObject] = []
    for item in legacy:
        if (item.canonical_url, item.issue_date) in selected_keys:
            continue
        record: JsonObject = {
            "legacyId": item.legacy_id,
            "issueDate": item.issue_date,
            "canonicalUrl": item.canonical_url,
            "title": item.title,
            "publishedAt": item.published_at,
            "publicationDateStatus": item.publication_date_status,
        }
        if not within_v2_publication_window(item):
            outside_window.append(record)
            continue
        kept = earliest_selection.get(item.canonical_url)
        if kept is not None and kept < item.issue_date:
            # A URL Legacy carried into a later issue while V2 kept only the first
            # appearance: the recorded historical-duplicate correction, still visible in
            # the release ledger as an `operation = 'correction'` entry.
            record["keptInIssueDate"] = kept
            historical_duplicates.append(record)
            continue
        unexplained.append(record)
    only_v2 = sorted(selected_keys - {(item.canonical_url, item.issue_date) for item in legacy})
    checks.add(
        "legacy_to_v2.every_dropped_row_is_explained",
        expected=0,
        actual=len(unexplained),
        detail=(
            "A Legacy row absent from the V2 selection must be refused by the "
            f"{V2_PUBLICATION_WINDOW_DAYS}-day publication window, or be a repeat carry whose "
            "earlier appearance V2 kept. Anything else is undiagnosed drift between the two "
            "contours."
        ),
    )
    checks.add(
        "legacy_to_v2.v2_publishes_nothing_legacy_never_saw",
        expected=0,
        actual=len(only_v2),
        detail=(
            "V2 selects from what Legacy produced. A selection Legacy has no row for cannot "
            "be explained by any V2 filter and always means a real defect."
        ),
    )
    return {
        "legacyRows": len(legacy),
        "v2SelectionRows": len(selected),
        "droppedOutsidePublicationWindow": outside_window,
        "droppedAsHistoricalDuplicate": historical_duplicates,
        "droppedUnexplained": unexplained,
        "selectedWithoutLegacyRow": [
            {"canonicalUrl": url, "issueDate": issue_date} for url, issue_date in only_v2
        ],
    }


def _normalization_layer(
    legacy: Sequence[LegacyMaterial],
    selected: Sequence[SelectedMaterial],
    checks: _Checks,
) -> JsonObject:
    """Legacy canonicalizes URLs; KX normalizes them again. Prove the two agree here.

    They are not the same function - Legacy strips ``www.`` and trailing slashes, KX does
    neither - so agreement is an observed property of this population, not an invariant.
    The check exists to notice the day it stops holding.
    """
    rewritten: list[JsonObject] = []
    rejected: list[JsonObject] = []
    for url in sorted({item.canonical_url for item in legacy}):
        normalized, error = _normalized(url)
        if error is not None:
            rejected.append({"canonicalUrl": url, "error": error})
        elif normalized != url:
            rewritten.append({"canonicalUrl": url, "normalized": normalized})
    checks.add(
        "normalization.kx_does_not_rewrite_legacy_canonical_urls",
        expected=0,
        actual=len(rewritten) + len(rejected),
        detail=(
            "KX re-normalizes whatever Legacy calls canonical. While that rewrites nothing, "
            "a URL is one key across all four stores; once it rewrites something, the stores "
            "no longer share a join key and every count below becomes incomparable."
        ),
    )
    document_ids = {document_id(_normalized(item.canonical_url)[0]) for item in selected}
    return {
        "rewrittenByKx": rewritten,
        "rejectedByKx": rejected,
        "selectionDistinctDocumentIds": len(document_ids),
    }


def _kx_layer(kx: JsonObject, selected: Sequence[SelectedMaterial], checks: _Checks) -> JsonObject:
    counts: JsonObject = kx["counts"]
    sources: list[JsonObject] = list(kx["perimeterSources"])
    members: list[JsonObject] = list(kx["perimeterMembers"])
    perimeter_documents: list[JsonObject] = list(kx["perimeterDocuments"])
    current = max(sources, key=lambda item: str(item["capturedAt"])) if sources else None
    current_id = str(current["perimeterSourceId"]) if current is not None else ""
    current_members = [m for m in members if str(m["perimeterSourceId"]) == current_id]
    current_documents = {str(m["documentId"]) for m in current_members}
    union_documents = {str(m["documentId"]) for m in members}
    historical_only = sorted(union_documents - current_documents)

    expected_documents = {document_id(_normalized(item.canonical_url)[0]) for item in selected}
    checks.add(
        "kx.current_snapshot_matches_the_active_release_selection",
        expected=len(selected),
        actual=len(current_members),
        detail=(
            "The newest perimeter snapshot is an import of the active content release; a "
            "different row count means the snapshot is stale or the import lost rows."
        ),
    )
    checks.add(
        "kx.perimeter_documents_are_the_selection_deduplicated_by_url",
        expected=len(expected_documents),
        actual=len(current_documents),
        detail=(
            "A document id is sha256 of the normalized canonical URL, so a URL selected into "
            "two issues is one document. Recomputing the ids from the release must reproduce "
            "exactly the set KX holds."
        ),
    )
    checks.add(
        "kx.perimeter_document_ids_are_reproducible_from_the_release",
        expected=0,
        actual=len(current_documents ^ expected_documents),
        detail=(
            "Set difference, not just cardinality: equal counts over different documents would "
            "otherwise pass silently."
        ),
    )
    checks.add(
        "kx.perimeter_full_text_is_complete",
        expected=len(current_documents),
        actual=sum(
            1
            for item in perimeter_documents
            if bool(item["hasCompleteVersion"]) and str(item["documentId"]) in current_documents
        ),
        detail=(
            "Perimeter completeness is the precondition of every coverage metric: a perimeter "
            "document without a complete version cannot support a quotation."
        ),
    )
    checks.add(
        "kx.documents_reconcile_with_materials",
        expected=int(counts["documents"]),
        actual=int(counts["materialDocumentsDistinctDocuments"])
        + int(counts["documentsWithoutMaterial"]),
        detail=(
            "Every document either belongs to an imported material or was created directly by "
            "the perimeter import. There is no third way in."
        ),
    )
    return {
        "unit": "document keyed by sha256(normalize_url(canonical_url))",
        "sourceOfTruth": "kx.issue_perimeter_members -> kx.documents",
        "schemaVersion": kx.get("schemaVersion"),
        "counts": counts,
        "corpusImports": kx["corpusImports"],
        "currentPerimeterSourceId": current_id,
        "perimeterSources": sources,
        "currentPerimeterMembers": len(current_members),
        "currentPerimeterDocuments": len(current_documents),
        "unionPerimeterDocuments": len(union_documents),
        "historicalOnlyDocuments": historical_only,
        "documentsWithoutMaterial": [
            item for item in kx["documentIndex"] if not bool(item["hasMaterial"])
        ],
        "perimeterDocumentsMissingFullText": [
            item
            for item in perimeter_documents
            if not bool(item["hasCompleteVersion"]) and str(item["documentId"]) in current_documents
        ],
    }


def _file_store_layer(
    discovery: Sequence[DiscoveryRecord],
    fulltext: Sequence[FulltextRecord],
    legacy: Sequence[LegacyMaterial],
    kx: JsonObject | None,
    checks: _Checks,
) -> JsonObject:
    discovery_urls = {item.canonical_url for item in discovery}
    legacy_urls = {item.canonical_url for item in legacy}
    absent_from_discovery = sorted(legacy_urls - discovery_urls)
    checks.add(
        "discovery.ids_are_unique",
        expected=len(discovery),
        actual=len({item.discovery_id for item in discovery}),
        detail="The registry is keyed by id; a repeat means an append went wrong.",
    )
    mismatched_keys = [
        item.cache_key
        for item in fulltext
        if item.canonical_url and fulltext_cache_key(item.canonical_url) != item.cache_key
    ]
    checks.add(
        "fulltext.file_names_match_their_canonical_url",
        expected=0,
        actual=len(mismatched_keys),
        detail=(
            "The cache key is sha256(canonical_url)[:24]. A file whose name does not match its "
            "content cannot be looked up by URL, so reconciliation would silently skip it."
        ),
    )

    result: JsonObject = {
        "discovery": {
            "unit": "discovery record",
            "sourceOfTruth": "knowledge/agpm-radar/data/materials.jsonl",
            "records": len(discovery),
            "distinctIds": len({item.discovery_id for item in discovery}),
            "distinctCanonicalUrls": len(discovery_urls),
            "byPerimeter": dict(
                sorted(
                    Counter(item.perimeter for item in discovery).items(),
                    key=lambda pair: str(pair[0]),
                )
            ),
            "duplicateCanonicalUrls": sorted(_duplicates(item.canonical_url for item in discovery)),
            "legacyUrlsAbsentFromRegistry": absent_from_discovery,
        },
        "fulltext": {
            "unit": "extracted-text file",
            "sourceOfTruth": "knowledge/agpm-radar/data/source-fulltext/",
            "files": len(fulltext),
            "withText": sum(1 for item in fulltext if item.text_chars > 0),
            "byStatus": dict(sorted(Counter(item.status for item in fulltext).items())),
        },
    }
    if kx is None:
        return result

    index: dict[str, JsonObject] = {str(item["documentId"]): item for item in kx["documentIndex"]}
    discovery_missing_in_kx: list[str] = []
    for url in sorted(discovery_urls):
        normalized, error = _normalized(url)
        if error is not None:
            continue
        if document_id(normalized) not in index:
            discovery_missing_in_kx.append(url)
    fulltext_missing_in_kx: list[JsonObject] = []
    for item in fulltext:
        if not item.canonical_url:
            continue
        normalized, error = _normalized(item.canonical_url)
        if error is not None:
            continue
        entry = index.get(document_id(normalized))
        if entry is None or not bool(entry["hasCompleteVersion"]):
            fulltext_missing_in_kx.append(
                {
                    "cacheKey": item.cache_key,
                    "canonicalUrl": item.canonical_url,
                    "status": item.status,
                    "textChars": item.text_chars,
                    "inKx": entry is not None,
                }
            )
    corpus_imports: list[JsonObject] = list(kx["corpusImports"])
    latest = max(corpus_imports, key=lambda item: str(item["importedAt"])) if corpus_imports else {}
    result["discovery"]["notInKx"] = discovery_missing_in_kx
    result["discovery"]["kxCorpusSnapshotRows"] = latest.get("rowCount")
    result["discovery"]["rowsAddedSinceKxSnapshot"] = len(discovery) - int(
        latest.get("rowCount", len(discovery))
    )
    result["fulltext"]["withoutCompleteVersionInKx"] = fulltext_missing_in_kx
    return result


def build_report(
    *,
    legacy: Sequence[LegacyMaterial],
    legacy_metadata_urls: Sequence[str],
    release: V2Release,
    discovery: Sequence[DiscoveryRecord],
    fulltext: Sequence[FulltextRecord],
    kx: JsonObject | None,
    inputs: Mapping[str, str],
) -> JsonObject:
    """Reconcile every layer and return the report plus the checks that gate it."""
    checks = _Checks()
    selected = release.selected
    report: JsonObject = {
        "inputs": dict(sorted(inputs.items())),
        "layers": {
            "discovery": None,
            "legacy": _legacy_layer(legacy, checks),
            "v2Release": _v2_layer(legacy, release, legacy_metadata_urls, checks),
            "kx": None,
        },
        "transitions": {
            "legacyToV2Selection": _legacy_to_v2_transition(legacy, selected, checks),
            "urlNormalization": _normalization_layer(legacy, selected, checks),
        },
    }
    if kx is not None:
        report["layers"]["kx"] = _kx_layer(kx, selected, checks)
    file_stores = _file_store_layer(discovery, fulltext, legacy, kx, checks)
    report["layers"]["discovery"] = file_stores["discovery"]
    report["layers"]["fulltext"] = file_stores["fulltext"]
    report["checks"] = [check.as_json() for check in checks.items]
    report["status"] = "ok" if all(check.ok for check in checks.items) else "failed"
    return report
