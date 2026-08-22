"""Choose the 20-30 documents the vertical slice runs on, and say why each is there.

Slice 2.1's precondition. The plan asks for a sample that is representative in
named ways - both languages, several genres and source families, a short and a
long document, one from a web archive, one from an operator artifact, one with
numbers in it, and a pair of known reprints of one primary source - and a sample
picked by hand satisfies that list only by accident.

So the list is code. Each requirement is a predicate with a minimum, selection is
greedy over the requirements in order, and the result records which requirement
each document is there for. A requirement that cannot be met is reported as a gap
rather than quietly dropped: the slice would otherwise measure a class it does not
actually contain.

Duplicates are found from hashes rather than from text. The extract sends the
canonical text hash and sixteen evenly spaced window hashes, computed on Local Ru,
so the reprint pair the plan asks for is identifiable without a single article
leaving the host that stores it.

Running this against the perimeter turned up two things the plan did not expect,
and both are recorded rather than smoothed over: there is no cross-host reprint in
the perimeter at all, and nine of its documents share one 215-character text that
is the YouTube page footer - counted as complete full text, and unable to support
any claim.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Two documents sharing this many of their sixteen windows are the same text.
#: Three is well past coincidence - a window is 120 characters - while tolerating
#: the boilerplate differences between one outlet's copy and another's.
REPRINT_SHARED_WINDOWS = 3

#: A duplicate group at least this large, whose text is at most
#: ``BOILERPLATE_MAX_CHARS``, is site chrome rather than a document. Nine YouTube
#: pages in the perimeter share one 215-character footer.
BOILERPLATE_GROUP_SIZE = 3
BOILERPLATE_MAX_CHARS = 1_000

#: A document is numerically dense when it carries at least this many runs of
#: digits per thousand characters. A page that mentions one year is not a page
#: whose numbers the verifier has to check.
NUMERIC_RUNS_PER_1000 = 12

SHORT_DOCUMENT_CHARS = 2_500
LONG_DOCUMENT_CHARS = 40_000


@dataclass(frozen=True, slots=True)
class Candidate:
    document_id: str
    version_id: str
    text_sha256: str
    canonical_url: str
    host: str
    title: str
    language: str
    quality: str
    source_kind: str
    chars: int
    number_runs: int
    chunks: int
    selections: int
    perimeters: str
    key_material: bool
    access_method: str | None
    archive_used: bool
    manual_review_required: bool
    window_signature: tuple[str, ...]

    @property
    def numeric_density(self) -> float:
        return (self.number_runs * 1000 / self.chars) if self.chars else 0.0

    def as_json(self) -> dict[str, Any]:
        return {
            "documentId": self.document_id,
            "versionId": self.version_id,
            "canonicalUrl": self.canonical_url,
            "host": self.host,
            "title": self.title,
            "language": self.language,
            "quality": self.quality,
            "sourceKind": self.source_kind,
            "chars": self.chars,
            "chunks": self.chunks,
            "numericDensity": round(self.numeric_density, 2),
            "selections": self.selections,
            "perimeters": self.perimeters,
            "keyMaterial": self.key_material,
            "accessMethod": self.access_method,
            "manualReviewRequired": self.manual_review_required,
        }


def load_candidates(payload: dict[str, Any]) -> tuple[Candidate, ...]:
    return tuple(
        Candidate(
            document_id=str(item["documentId"]),
            version_id=str(item["versionId"]),
            text_sha256=str(item["textSha256"]),
            canonical_url=str(item["canonicalUrl"]),
            host=str(item["host"] or ""),
            title=str(item["title"] or ""),
            language=str(item["language"]),
            quality=str(item["quality"]),
            source_kind=str(item["sourceKind"]),
            chars=int(item["chars"]),
            number_runs=int(item["numberRuns"]),
            chunks=int(item["chunks"]),
            selections=int(item["selections"]),
            perimeters=str(item["perimeters"] or ""),
            key_material=bool(item["keyMaterial"]),
            access_method=(
                str(item["accessMethod"]) if item.get("accessMethod") is not None else None
            ),
            archive_used=bool(item.get("archiveUsed")),
            manual_review_required=bool(item.get("manualReviewRequired")),
            window_signature=tuple(str(value) for value in (item["windowSignature"] or ())),
        )
        for item in payload["documents"]
    )


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """Documents that carry the same text."""

    members: tuple[Candidate, ...]
    #: ``exact`` when the canonical text hashes agree; ``near`` when only the
    #: window signatures do.
    kind: str

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset(item.host for item in self.members)

    @property
    def cross_host(self) -> bool:
        """A reprint is republication somewhere else. One site is not a reprint."""
        return len(self.hosts) > 1

    @property
    def boilerplate(self) -> bool:
        return (
            len(self.members) >= BOILERPLATE_GROUP_SIZE
            and max(item.chars for item in self.members) <= BOILERPLATE_MAX_CHARS
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "crossHost": self.cross_host,
            "boilerplate": self.boilerplate,
            "hosts": sorted(self.hosts),
            "chars": max(item.chars for item in self.members),
            "documents": [item.canonical_url for item in self.members],
        }


def find_duplicate_groups(
    candidates: Sequence[Candidate], *, shared_windows: int = REPRINT_SHARED_WINDOWS
) -> tuple[DuplicateGroup, ...]:
    """Group documents that carry the same text, by hash first and windows second."""
    by_hash: dict[str, list[Candidate]] = {}
    for item in candidates:
        by_hash.setdefault(item.text_sha256, []).append(item)
    exact = {
        frozenset(item.document_id for item in members)
        for members in by_hash.values()
        if len(members) > 1
    }
    groups = [
        DuplicateGroup(
            members=tuple(sorted(members, key=lambda item: item.canonical_url)), kind="exact"
        )
        for members in by_hash.values()
        if len(members) > 1
    ]
    for cluster in _window_clusters(candidates, shared_windows=shared_windows):
        identifiers = frozenset(item.document_id for item in cluster)
        if identifiers not in exact:
            groups.append(DuplicateGroup(members=cluster, kind="near"))
    return tuple(sorted(groups, key=lambda group: group.members[0].canonical_url))


def _window_clusters(
    candidates: Sequence[Candidate], *, shared_windows: int = REPRINT_SHARED_WINDOWS
) -> tuple[tuple[Candidate, ...], ...]:
    """Group documents whose window signatures overlap. Union-find over pairs."""
    parent: dict[str, str] = {item.document_id: item.document_id for item in candidates}

    def root(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for index, left in enumerate(candidates):
        left_windows = set(left.window_signature)
        for right in candidates[index + 1 :]:
            if len(left_windows & set(right.window_signature)) >= shared_windows:
                parent[root(right.document_id)] = root(left.document_id)

    groups: dict[str, list[Candidate]] = {}
    for item in candidates:
        groups.setdefault(root(item.document_id), []).append(item)
    return tuple(
        tuple(sorted(members, key=lambda item: item.canonical_url))
        for _, members in sorted(groups.items())
        if len(members) > 1
    )


@dataclass(frozen=True, slots=True)
class Requirement:
    name: str
    why: str
    minimum: int
    predicate: Callable[[Candidate], bool]


def requirements(
    duplicates: frozenset[str], cross_host: frozenset[str], boilerplate: frozenset[str]
) -> tuple[Requirement, ...]:
    """The sample properties plan §13.1 names, as predicates with minimums.

    Every requirement except ``boilerplate_negative`` refuses a boilerplate
    document. Site chrome is English, short, in some perimeter and technically
    complete, so it would satisfy half the list while measuring nothing.
    """
    defined = (
        Requirement(
            "web_archive",
            "text taken from an archive snapshot: the citation rule differs, and four "
            "documents are currently unquotable for exactly that reason",
            1,
            lambda item: item.access_method == "web_archive",
        ),
        Requirement(
            "operator_artifact",
            "text that arrived as a file rather than a response, so provenance is the "
            "only record of where it came from",
            2,
            lambda item: item.access_method == "operator_file",
        ),
        Requirement(
            "not_an_ordinary_request",
            "obtained by escalating past an ordinary request - the D9 pair",
            1,
            lambda item: item.access_method == "browser_headers",
        ),
        Requirement(
            "cross_host_reprint",
            "the same text republished by a different outlet: the source-independence "
            "rule of ADR-0007 has to be exercised against a real reprint",
            2,
            lambda item: item.document_id in cross_host,
        ),
        Requirement(
            "content_duplicate_pair",
            "two documents carrying the same text on one site: a duplicate cluster, "
            "which counts as one confirmation even without a second source family",
            2,
            lambda item: item.document_id in duplicates and item.document_id not in boilerplate,
        ),
        Requirement(
            "boilerplate_negative",
            "a document whose complete text is site chrome. Included deliberately: the "
            "slice has to show that nothing builds evidence on it, and nine such "
            "documents are inside the perimeter today",
            1,
            lambda item: item.document_id in boilerplate,
        ),
        Requirement(
            "language_ru",
            "Russian: the product language, and a different stemmer",
            5,
            lambda item: item.language == "ru",
        ),
        Requirement(
            "language_en",
            "English: most of the corpus",
            8,
            lambda item: item.language == "en",
        ),
        Requirement(
            "short_document",
            "a short document: chunking, retrieval and abstention all behave "
            "differently when there is barely any text",
            2,
            lambda item: item.chars <= SHORT_DOCUMENT_CHARS and item.document_id not in boilerplate,
        ),
        Requirement(
            "long_document",
            "a long document: several chunks, so retrieval has to pick the right one",
            2,
            lambda item: item.chars >= LONG_DOCUMENT_CHARS,
        ),
        Requirement(
            "numeric",
            "numbers to check: the zero-tolerance figure comparison needs something to compare",
            3,
            lambda item: item.numeric_density >= NUMERIC_RUNS_PER_1000,
        ),
        Requirement(
            "perimeter_near",
            "near perimeter: directly about agentic project management",
            3,
            lambda item: "near" in item.perimeters,
        ),
        Requirement(
            "perimeter_mid",
            "mid perimeter",
            3,
            lambda item: "mid" in item.perimeters,
        ),
        Requirement(
            "perimeter_far",
            "far perimeter: adjacent material, where relevance is hardest",
            3,
            lambda item: "far" in item.perimeters,
        ),
        Requirement(
            "key_material",
            "material the editor marked key: what a reader is most likely to ask about",
            2,
            lambda item: item.key_material,
        ),
        Requirement(
            "multi_issue",
            "one document selected into more than one issue: the 277-versus-275 case, "
            "where the unit of counting actually matters",
            1,
            lambda item: item.selections > 1,
        ),
    )
    return tuple(
        item
        if item.name == "boilerplate_negative"
        else Requirement(
            item.name,
            item.why,
            item.minimum,
            _excluding(item.predicate, boilerplate),
        )
        for item in defined
    )


def _excluding(
    predicate: Callable[[Candidate], bool], excluded: frozenset[str]
) -> Callable[[Candidate], bool]:
    return lambda item: item.document_id not in excluded and predicate(item)


@dataclass(slots=True)
class Selection:
    chosen: list[Candidate] = field(default_factory=list)
    reasons: dict[str, list[str]] = field(default_factory=dict)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    duplicate_groups: list[dict[str, Any]] = field(default_factory=list)
    population: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "size": len(self.chosen),
            "hosts": len({item.host for item in self.chosen}),
            "population": self.population,
            "documents": [
                {**item.as_json(), "selectedFor": self.reasons.get(item.document_id, [])}
                for item in self.chosen
            ],
            "requirementGaps": self.gaps,
            "duplicateGroups": self.duplicate_groups,
        }


def select(candidates: Sequence[Candidate], *, size: int = 24) -> Selection:
    """Pick a sample that provably covers every named requirement.

    Greedy over the requirements in order, preferring a document from a host that
    is not represented yet - distinct hosts are the cheapest available proxy for
    distinct source families until slice 2.4 assigns real ones.
    """
    groups = find_duplicate_groups(candidates)
    duplicates = frozenset(item.document_id for group in groups for item in group.members)
    cross_host = frozenset(
        item.document_id for group in groups if group.cross_host for item in group.members
    )
    boilerplate = frozenset(
        item.document_id for group in groups if group.boilerplate for item in group.members
    )
    selection = Selection(
        duplicate_groups=[group.as_json() for group in groups],
        population={
            "documents": len(candidates),
            "distinctTexts": len({item.text_sha256 for item in candidates}),
            "documentsSharingAText": len(duplicates),
            "boilerplateDocuments": len(boilerplate),
            "crossHostReprints": len(cross_host),
            "hosts": len({item.host for item in candidates}),
        },
    )
    chosen: dict[str, Candidate] = {}
    hosts: set[str] = set()

    def take(item: Candidate, reason: str) -> None:
        chosen.setdefault(item.document_id, item)
        hosts.add(item.host)
        selection.reasons.setdefault(item.document_id, []).append(reason)

    def rank(item: Candidate) -> tuple[int, int, str]:
        # A new host first, then the more substantial document, then a stable tie
        # break so the same corpus always yields the same sample.
        return (0 if item.host in hosts else -1, -item.chars, item.canonical_url)

    for requirement in requirements(duplicates, cross_host, boilerplate):
        matching = [item for item in candidates if requirement.predicate(item)]
        have = [item for item in matching if item.document_id in chosen]
        for item in have:
            selection.reasons.setdefault(item.document_id, []).append(requirement.name)
        needed = requirement.minimum - len(have)
        if needed <= 0:
            continue
        available = sorted((item for item in matching if item.document_id not in chosen), key=rank)
        if len(available) < needed:
            selection.gaps.append(
                {
                    "requirement": requirement.name,
                    "why": requirement.why,
                    "wanted": requirement.minimum,
                    "available": len(have) + len(available),
                }
            )
        for item in available[:needed]:
            take(item, requirement.name)

    # Fill the remainder with host diversity, longest first, so the sample is not
    # dominated by one outlet.
    for item in sorted(candidates, key=rank):
        if len(chosen) >= size:
            break
        if item.document_id in chosen or item.document_id in boilerplate:
            continue
        take(item, "diversity_fill")
        hosts.add(item.host)

    selection.chosen = sorted(chosen.values(), key=lambda item: item.canonical_url)
    return selection
