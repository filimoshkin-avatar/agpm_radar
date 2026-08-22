"""Read-only inventory of the AgPM file wiki the Project Manager maintains.

Slice 1.5 of the knowledge-base plan. The wiki is not a thing we are building - it exists,
it is written by hand, and the product is a published projection of it backed by evidence
from KX. Before anything can be projected we need to know, page by page, what is actually
there: which of the SCHEMA.md conventions a page follows, what its atomic claims are, and
whether it cites a source at all.

This module reads and never writes. It computes:

* a **register** of every file under both knowledge roots, classified by layer;
* per page, which canonical SCHEMA.md sections are present - matching the bilingual
  headings the wiki actually uses, not only the English ones the schema names;
* **atomic claim candidates**: the list items under a claim-bearing section, which is the
  granularity ``concept_claims`` will need;
* **evidence posture**: whether the page names supporting sources, links out to a URL, or
  asserts with nothing behind it;
* the **link graph** between pages, including links that point at a file that is not there.

The output is deliberately a measurement rather than a judgement. Conformance to the page
conventions is reported as a number per page; nothing is rewritten and nothing is scored.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

#: The six page conventions of ``agpm/SCHEMA.md``. The wiki is written in two languages and
#: paraphrases its own headings, so each canonical section carries the substrings that
#: actually appear. Matching is on a casefolded, punctuation-stripped heading.
SECTION_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("purpose", ("purpose", "назначение", "смысл источника", "role in corpus", "статус в корпусе")),
    (
        "core_claims",
        (
            "core claims",
            "ключевые тезисы",
            "ключевой тезис",
            "основные тезисы",
            "краткое содержание",
            "main contribution to the agpm corpus",
            "основной вклад в корпус agpm",
            "why this source matters",
            "почему источник важен",
            "why these principles matter",
        ),
    ),
    (
        "supporting_sources",
        (
            "supporting sources",
            "поддерживающие источники",
            "источник",
            "источники",
            "метаданные",
            "стартовые строки",
            "opening lines",
        ),
    ),
    (
        "tensions",
        (
            "tensions / contradictions",
            "tensions",
            "напряжения / противоречия",
            "противоречия",
            "contradictions",
        ),
    ),
    (
        "implications",
        (
            "implications for agpm model",
            "implications for the model",
            "implications",
            "следствия для модели agpm",
            "импликации для agpm",
            "практические следствия",
            "практический вывод",
            "практическое правило для корпуса",
            "best use in the knowledge base",
            "что можно импортировать в compiled wiki",
        ),
    ),
    (
        "open_questions",
        (
            "open questions",
            "открытые вопросы",
            "limits of this source for agpm",
            "ограничения источника",
        ),
    ),
)

#: The relationship vocabulary ``agpm/SCHEMA.md`` defines. Carried into the graph unchanged
#: (plan §10.6), so it matters whether the pages use it or only the schema names it.
RELATIONSHIP_TYPES = (
    "supports",
    "extends",
    "constrains",
    "contradicts",
    "operationalizes",
    "depends-on",
)

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+(.+)$")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BARE_URL = re.compile(r"(?<![(\[])\bhttps?://[^\s<>\")]+")
_PUNCTUATION = re.compile(r"[^\w\s/]+", re.UNICODE)
_EMPHASIS = re.compile(r"[*_`]+")

#: Directory -> layer. Longest matching relative prefix wins.
_LAYER_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("agpm/raw/originals", "raw_original"),
    ("agpm/raw", "raw_extract"),
    ("agpm/reviews", "review"),
    ("agpm/wiki/sources", "source_note"),
    ("agpm/wiki", "synthesis_page"),
    ("agpm-radar/data/source-fulltext", "fulltext_cache"),
    ("agpm-radar/data", "registry_data"),
    ("agpm-radar/reports", "daily_report"),
    ("agpm-radar/runs", "run_journal"),
    ("agpm-radar/wiki/daily", "daily_snapshot"),
    ("agpm-radar/wiki/monthly", "monthly_summary"),
    ("agpm-radar/wiki/stats", "stats"),
    ("agpm-radar/wiki/overview", "radar_overview"),
    ("agpm-radar/wiki", "synthesis_page"),
)

_ROOT_FILE_LAYERS = {
    "SCHEMA.md": "schema",
    "index.md": "index",
    "log.md": "log",
    "incidents.md": "log",
    "sources.yml": "registry_data",
}

#: Layers whose pages carry authored assertions, i.e. the ones slice 2.5 has to bind to
#: evidence. Everything else is raw material, a journal, or generated output.
AUTHORED_LAYERS = frozenset({"synthesis_page", "source_note", "radar_overview", "monthly_summary"})


def _normalize_heading(text: str) -> str:
    stripped = _EMPHASIS.sub("", text)
    stripped = _PUNCTUATION.sub(" ", stripped)
    return " ".join(unicodedata.normalize("NFC", stripped).casefold().split())


def canonical_section(heading: str) -> str | None:
    """Map a heading, in either language, onto one of the six SCHEMA.md conventions."""
    normalized = _normalize_heading(heading)
    if not normalized:
        return None
    for section, aliases in SECTION_ALIASES:
        for alias in aliases:
            if normalized == alias or normalized.startswith(f"{alias} "):
                return section
    return None


@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    text: str
    section: str | None
    line: int


@dataclass(frozen=True, slots=True)
class Claim:
    """One list item under a claim-bearing section: the granularity of ``concept_claims``."""

    section: str
    heading: str
    line: int
    text: str


@dataclass(frozen=True, slots=True)
class Link:
    target: str
    resolved: str | None
    exists: bool


@dataclass(frozen=True, slots=True)
class Page:
    relative_path: str
    layer: str
    section_directory: str
    title: str
    bytes: int
    sha256: str
    lines: int
    words: int
    headings: tuple[Heading, ...]
    sections_present: tuple[str, ...]
    claims: tuple[Claim, ...]
    internal_links: tuple[Link, ...]
    external_urls: tuple[str, ...]
    relationship_terms: tuple[str, ...]

    @property
    def cites_sources(self) -> bool:
        """A page counts as citing something if it names sources or links out of the wiki."""
        return "supporting_sources" in self.sections_present or bool(self.external_urls)

    def as_json(self) -> JsonObject:
        return {
            "relativePath": self.relative_path,
            "layer": self.layer,
            "sectionDirectory": self.section_directory,
            "title": self.title,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "lines": self.lines,
            "words": self.words,
            "headings": [
                {"level": item.level, "text": item.text, "section": item.section, "line": item.line}
                for item in self.headings
            ],
            "sectionsPresent": list(self.sections_present),
            "sectionsMissing": [
                name for name, _ in SECTION_ALIASES if name not in self.sections_present
            ],
            "conventionConformance": len(self.sections_present) / len(SECTION_ALIASES),
            "claimCount": len(self.claims),
            "claims": [
                {
                    "section": item.section,
                    "heading": item.heading,
                    "line": item.line,
                    "text": item.text,
                }
                for item in self.claims
            ],
            "internalLinks": [
                {"target": item.target, "resolved": item.resolved, "exists": item.exists}
                for item in self.internal_links
            ],
            "brokenLinks": [item.target for item in self.internal_links if not item.exists],
            "externalUrls": list(self.external_urls),
            "relationshipTerms": list(self.relationship_terms),
            "citesSources": self.cites_sources,
        }


def layer_for(relative_path: str) -> str:
    name = Path(relative_path).name
    root_relative = "/".join(Path(relative_path).parts[1:])
    if root_relative in _ROOT_FILE_LAYERS:
        return _ROOT_FILE_LAYERS[root_relative]
    if name in _ROOT_FILE_LAYERS and len(Path(relative_path).parts) == 2:
        return _ROOT_FILE_LAYERS[name]
    best = ""
    layer = "other"
    for prefix, candidate in _LAYER_BY_PREFIX:
        if relative_path.startswith(f"{prefix}/") and len(prefix) > len(best):
            best, layer = prefix, candidate
    return layer


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fenced_lines(lines: Sequence[str]) -> frozenset[int]:
    """Line numbers inside a fenced code block, fence markers included.

    A wiki page that documents markdown - and several of them do - would otherwise
    contribute its examples as headings and claims.
    """
    inside = False
    fenced: set[int] = set()
    for number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            fenced.add(number)
            inside = not inside
            continue
        if inside:
            fenced.add(number)
    return frozenset(fenced)


def iter_headings(lines: Sequence[str], fenced: frozenset[int]) -> Iterator[Heading]:
    for number, line in enumerate(lines, start=1):
        if number in fenced:
            continue
        match = HEADING.match(line)
        if match is None:
            continue
        text = match.group(2).strip()
        yield Heading(
            level=len(match.group(1)),
            text=text,
            section=canonical_section(text),
            line=number,
        )


def claim_sections(headings: Sequence[Heading]) -> dict[int, tuple[str, str]]:
    """Line number -> (canonical section, heading text) for sections that carry claims."""
    wanted = {"core_claims", "implications", "tensions", "open_questions"}
    return {
        heading.line: (heading.section, heading.text)
        for heading in headings
        if heading.section in wanted and heading.section is not None
    }


def _read_page(path: Path, relative_path: str, roots: dict[str, Path]) -> Page:
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    fenced = fenced_lines(lines)
    headings = tuple(iter_headings(lines, fenced))
    title = next((item.text for item in headings if item.level == 1), path.stem)

    starts = claim_sections(headings)
    boundaries = sorted(item.line for item in headings)
    claims: list[Claim] = []
    for start, (section, heading_text) in sorted(starts.items()):
        following = next((line for line in boundaries if line > start), len(lines) + 1)
        for number in range(start + 1, min(following, len(lines) + 1)):
            if number in fenced:
                continue
            match = LIST_ITEM.match(lines[number - 1])
            if match is None:
                continue
            claim = _EMPHASIS.sub("", match.group(1)).strip()
            if claim:
                claims.append(Claim(section=section, heading=heading_text, line=number, text=claim))

    internal: list[Link] = []
    external: list[str] = []
    for _, target in _MARKDOWN_LINK.findall(text):
        if target.startswith(("http://", "https://")):
            external.append(target)
            continue
        if target.startswith("#") or target.startswith("mailto:"):
            continue
        candidate = (path.parent / target.split("#", 1)[0]).resolve()
        resolved: str | None = None
        for root_name, root in roots.items():
            try:
                resolved = f"{root_name}/{candidate.relative_to(root).as_posix()}"
            except ValueError:
                continue
            break
        internal.append(Link(target=target, resolved=resolved, exists=candidate.exists()))
    external.extend(_BARE_URL.findall(text))

    lowered = text.casefold()
    relationship_terms = tuple(term for term in RELATIONSHIP_TYPES if term in lowered)

    return Page(
        relative_path=relative_path,
        layer=layer_for(relative_path),
        section_directory="/".join(Path(relative_path).parts[:-1]),
        title=title,
        bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        lines=len(lines),
        words=len(text.split()),
        headings=headings,
        sections_present=tuple(
            sorted({item.section for item in headings if item.section is not None})
        ),
        claims=tuple(claims),
        internal_links=tuple(internal),
        external_urls=tuple(dict.fromkeys(external)),
        relationship_terms=relationship_terms,
    )


def collect(roots: dict[str, Path]) -> tuple[tuple[Page, ...], tuple[JsonObject, ...]]:
    """Read every file under the knowledge roots. Markdown is parsed; the rest is weighed."""
    pages: list[Page] = []
    assets: list[JsonObject] = []
    resolved_roots = {name: root.resolve() for name, root in roots.items()}
    for name, root in sorted(resolved_roots.items()):
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative_path = f"{name}/{path.relative_to(root).as_posix()}"
            if path.suffix.lower() == ".md":
                pages.append(_read_page(path, relative_path, resolved_roots))
                continue
            assets.append(
                {
                    "relativePath": relative_path,
                    "layer": layer_for(relative_path),
                    "bytes": path.stat().st_size,
                    "suffix": path.suffix.lower(),
                    "sha256": _sha256_file(path) if path.stat().st_size < (1 << 24) else None,
                }
            )
    return tuple(pages), tuple(assets)


def build_inventory(pages: Sequence[Page], assets: Sequence[JsonObject]) -> JsonObject:
    """Summarize the register into the numbers slice 2.5 has to plan against."""
    authored = [page for page in pages if page.layer in AUTHORED_LAYERS]
    by_layer = Counter(page.layer for page in pages)
    by_section_directory = Counter(page.section_directory for page in pages)

    section_coverage = {
        name: sum(1 for page in authored if name in page.sections_present)
        for name, _ in SECTION_ALIASES
    }
    fully_conformant = [
        page.relative_path
        for page in authored
        if len(page.sections_present) == len(SECTION_ALIASES)
    ]
    without_any_convention = [page.relative_path for page in authored if not page.sections_present]
    without_sources = [page.relative_path for page in authored if not page.cites_sources]
    broken_links = [
        {"page": page.relative_path, "target": link.target}
        for page in pages
        for link in page.internal_links
        if not link.exists
    ]
    heading_vocabulary = Counter(
        item.text for page in authored for item in page.headings if item.level == 2
    )
    unmapped_headings = Counter(
        item.text
        for page in authored
        for item in page.headings
        if item.level == 2 and item.section is None
    )
    relationship_usage = Counter(term for page in authored for term in page.relationship_terms)

    return {
        "totals": {
            "markdownPages": len(pages),
            "nonMarkdownAssets": len(assets),
            "bytesMarkdown": sum(page.bytes for page in pages),
            "bytesAssets": sum(int(item["bytes"]) for item in assets),
            "authoredPages": len(authored),
            "authoredWords": sum(page.words for page in authored),
            "atomicClaimCandidates": sum(len(page.claims) for page in authored),
        },
        "byLayer": dict(sorted(by_layer.items())),
        "bySectionDirectory": dict(sorted(by_section_directory.items())),
        "emptySectionDirectories": [],
        "pageConventions": {
            "authoredPages": len(authored),
            "sectionCoverage": section_coverage,
            "fullyConformantPages": fully_conformant,
            "pagesWithNoRecognizedSection": without_any_convention,
            "distinctLevelTwoHeadings": len(heading_vocabulary),
            "unmappedLevelTwoHeadings": len(unmapped_headings),
            "mostCommonUnmappedHeadings": unmapped_headings.most_common(20),
        },
        "evidencePosture": {
            "pagesCitingSources": len(authored) - len(without_sources),
            "pagesWithoutAnySource": without_sources,
            "externalUrlsReferenced": len({url for page in authored for url in page.external_urls}),
        },
        "linkGraph": {
            "internalLinks": sum(len(page.internal_links) for page in pages),
            "brokenLinks": broken_links,
            "relationshipVocabularyUsage": dict(sorted(relationship_usage.items())),
        },
        "claimsByLayer": {
            layer: sum(len(page.claims) for page in authored if page.layer == layer)
            for layer in sorted({page.layer for page in authored})
        },
    }


def build_register(
    roots: dict[str, Path],
) -> JsonObject:
    """Read both knowledge roots and return the full register plus its summary."""
    pages, assets = collect(roots)
    inventory = build_inventory(pages, assets)
    empty_directories = sorted(
        f"{name}/{path.relative_to(root.resolve()).as_posix()}"
        for name, root in ((name, root.resolve()) for name, root in roots.items())
        for path in root.rglob("*")
        if path.is_dir() and not any(child.is_file() for child in path.rglob("*"))
    )
    inventory["emptySectionDirectories"] = empty_directories
    return {
        "roots": {name: str(root) for name, root in sorted(roots.items())},
        "inventory": inventory,
        "pages": [page.as_json() for page in pages],
        "assets": list(assets),
    }
