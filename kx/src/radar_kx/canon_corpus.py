"""Load the AgPM canon and the external standards into KX as their own corpus.

Slice 1.6. Radar's own pages assert things about agentic project management; the
canon is what those assertions are supposed to rest on. Until it is in KX with
exact offsets there is nothing to bind a wiki claim to, and the inventory of the
wiki (slice 1.5) measured the size of that hole: 27 of 63 authored pages cite
nothing at all, including the compiled model page itself.

Three properties this module exists to hold:

**The canon is a separate membership class.** It gets its own ``corpus_imports``
row with ``source_kind = 'canon_import'``, it never enters the issue perimeter,
and it never lands in a coverage denominator computed over Radar materials.

**Canon documents are never fetched.** They are files on this host. They get the
reserved ``agpm-canon:/`` identity, no ``fetch_queue`` row and no
``fetch_attempts`` row, because there is no request to record and inventing one
is the mistake that produced defect D9.

**Not every file is the source text.** ``agpm/raw/`` holds three different things
side by side: faithful conversions of a document, curated excerpts of one, and
notes we wrote about one. Only the first can back a quotation attributed to the
original - the other two are our own words. The fidelity of each file is declared
here, and anything but a faithful conversion is imported with provenance that
blocks public quotation. A file this table does not name cannot be imported at
all: a new canon document is a decision, not an accident.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radar_kx.artifact_import import ArtifactDocument, ArtifactManifest, import_artifact
from radar_kx.database import CorpusMember, Database, VersionProvenance
from radar_kx.identifiers import document_id
from radar_kx.url_policy import canon_url

#: How faithfully a file in ``agpm/raw/`` represents the source it names.
#:
#: ``full_text``
#:     a conversion of the document itself - a DOCX or PDF rendered to markdown.
#:     Quotable, with attribution, like any other converted source in KX.
#: ``extract``
#:     a curated excerpt, written by us, of a source we hold only in part.
#: ``note``
#:     our own summary of a source. Says what the source is about; contains none
#:     of its words.
CANON_FIDELITY: dict[str, str] = {
    # Faithful conversions of the AgPM canon.
    "agpm-white-paper-v1.0-d075a34c-37b1-46cd-878b-0e7c166e6ce1": "full_text",
    "agpm-white-paper-v1.2-a565755c-bfa2-4255-97cf-de5b43d25625": "full_text",
    "agpm-манифест-15083e77-7ea2-4594-aa2a-2a905d9978ee": "full_text",
    "agpm-манифест-v2-3ffb0559-841b-4375-bcc2-b18d520e50f1": "full_text",
    "agpm-манифест-v3-926ec919-b6bb-4058-a8d7-80b4053df422": "full_text",
    "agpm-обоснование-онтологии-1136cafc-0bcf-4cb7-9826-98cc0ceebf89": "full_text",
    "agpm-компоненты-по-уровням-2026-04-19": "full_text",
    "ценности-для-манифеста-f406b379-635b-4065-ab07-d793bdcc3577": "full_text",
    "agpm-industries-analytical-note-2026-05-02": "full_text",
    # The live AgPM implementation methodology and its companion artefacts.
    "agpm-live-implementation-methodology-v4-2-2026-07-13": "full_text",
    "agpm-live-method-agent-scenarios-v1-0-2026-07-13": "full_text",
    "agpm-live-method-api-contract-v1-3-2026-07-13": "full_text",
    "agpm-live-method-deployment-runbook-v1-3-2026-07-13": "full_text",
    "agpm-live-method-tool-readme-v1-3-2026-07-13": "full_text",
    # AI PMO implementation method, several revisions, plus its market research.
    "ai-pmo-implementation-method-kernel-v6-3-2026-06-14": "full_text",
    "ai-pmo-implementation-method-kit-v6-3-2026-06-14": "full_text",
    "ai-pmo-implementation-method-reference-v4-3-2026-06-14": "full_text",
    "ai-pmo-implementation-method-v1-8-2-2026-05-28": "full_text",
    "ai-pmo-implementation-method-v3-3-2026-05-31": "full_text",
    "ai-pmo-market-research-2026-06-05": "full_text",
    # External standards and industry documents held in full.
    "gost-r-72514-2026-full-text-2026-05-02": "full_text",
    "ngmn-agentic-ai-based-operating-models-v1.0-2026-03-24": "full_text",
    "sber-ai-disrupt-pdlc-full-docx-2026-06-14": "full_text",
    "sber-ai-disrupt-pdlc-whitepaper-2026-05": "full_text",
    # Curated excerpts. Our selection and our wording, so a quotation from one is
    # not a quotation from the standard.
    "agpmbok-v0.7-working-draft-extract": "extract",
    "aws-agentic-ai-business-extract": "extract",
    "iso-21502-2024-classical-reference-extract": "extract",
    "pmbok-8-2025-ai-automation-bridge-extract": "extract",
    "why-agpm-v3-executive-rationale-extract": "extract",
    # Notes about a source rather than any of its text.
    "agentic-business-process-management-arxiv-2603.18916v2": "note",
    "gost-r-72514-2026-public-reference-note-2026-05-02": "note",
    "toward-agentic-software-project-management-arxiv-2601.16392v1": "note",
}

_BLOCK_REASON = {
    "extract": (
        "this file is a curated excerpt written by us, not the text of the source; "
        "a quotation attributed to the original needs the original"
    ),
    "note": (
        "this file is our note about the source and contains none of its words; "
        "it cannot back a quotation attributed to the original"
    ),
}


class CanonCorpusError(ValueError):
    """The canon directory contains something the corpus definition does not cover."""


@dataclass(frozen=True, slots=True)
class CanonFile:
    path: Path
    relative_path: str
    canonical_url: str
    material_id: str
    title: str
    fidelity: str
    bytes: int
    sha256: str
    modified_at: datetime

    @property
    def quotable(self) -> bool:
        return self.fidelity == "full_text"

    def as_json(self) -> dict[str, Any]:
        return {
            "relativePath": self.relative_path,
            "canonicalUrl": self.canonical_url,
            "title": self.title,
            "fidelity": self.fidelity,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "modifiedAt": self.modified_at.isoformat(),
            "quotable": self.quotable,
        }


def _title(path: Path) -> str:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:1000]
    return path.stem


def scan_canon(raw_directory: Path) -> tuple[CanonFile, ...]:
    """Read ``agpm/raw/*.md`` and declare what each file is.

    Only markdown is read. ``raw/originals/`` holds the DOCX and PDF the markdown
    was converted from; the parser does not support DOCX and Docling was rejected
    (plan §11.1), so widening the format is a separate decision rather than a
    silent skip.
    """
    files: list[CanonFile] = []
    unknown: list[str] = []
    for path in sorted(raw_directory.glob("*.md")):
        fidelity = CANON_FIDELITY.get(path.stem)
        if fidelity is None:
            unknown.append(path.name)
            continue
        payload = path.read_bytes()
        relative_path = path.name
        files.append(
            CanonFile(
                path=path,
                relative_path=relative_path,
                canonical_url=canon_url(relative_path),
                material_id=f"canon:{relative_path}",
                title=_title(path),
                fidelity=fidelity,
                bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                modified_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
            )
        )
    if unknown:
        raise CanonCorpusError(
            "canon files with no declared fidelity: "
            + ", ".join(unknown)
            + ". Add them to CANON_FIDELITY - whether a file is the source text, our "
            "excerpt of it, or our note about it decides whether it may be quoted."
        )
    if not files:
        raise CanonCorpusError(f"no canon markdown found under {raw_directory}")
    return tuple(files)


def canon_corpus_sha256(files: Sequence[CanonFile]) -> str:
    """One hash over the whole corpus: path and content of every file, in order."""
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda file: file.relative_path):
        digest.update(f"{item.relative_path}\0{item.sha256}\n".encode())
    return digest.hexdigest()


def canon_provenance(item: CanonFile, *, provided_by: str) -> VersionProvenance:
    reason = _BLOCK_REASON.get(item.fidelity)
    return VersionProvenance(
        source_access_method="local_import",
        manual_review_required=reason is not None,
        manual_review_reason=reason,
        provided_by=provided_by,
        provided_at=item.modified_at,
        notes=f"AgPM canon, fidelity={item.fidelity}",
    )


def build_canon_artifact(
    files: Sequence[CanonFile],
    *,
    name: str,
    recorded_by: str,
    provided_by: str,
) -> ArtifactManifest:
    """Turn the scanned canon into the manifest the offline import path consumes."""
    return ArtifactManifest(
        name=name,
        recorded_by=recorded_by,
        documents=tuple(
            ArtifactDocument(
                canonical_url=item.canonical_url,
                path=item.path,
                content_type="text/plain; charset=utf-8",
                source_kind="local_import",
                fetched_at=item.modified_at,
                provenance=canon_provenance(item, provided_by=provided_by),
            )
            for item in files
        ),
    )


def import_canon(
    database: Database,
    files: Sequence[CanonFile],
    *,
    source_name: str,
    recorded_by: str,
    provided_by: str,
) -> dict[str, Any]:
    """Register the canon as its own corpus, then load the text of every file.

    Two steps in one command because they are one fact: the corpus row without the
    versions is a promise of evidence that does not exist, and the versions without
    the corpus row leave ``verify --full`` unable to reconcile anything.
    """
    corpus_sha256 = canon_corpus_sha256(files)
    registration = database.register_corpus_members(
        corpus_sha256=corpus_sha256,
        source_name=source_name,
        source_kind="canon_import",
        members=[
            CorpusMember(
                material_id=item.material_id,
                document_id=document_id(item.canonical_url),
                canonical_url=item.canonical_url,
                title=item.title,
                seen_at=item.modified_at,
                payload={
                    "relative_path": item.relative_path,
                    "fidelity": item.fidelity,
                    "bytes": item.bytes,
                    "sha256": item.sha256,
                    "title": item.title,
                },
            )
            for item in files
        ],
    )
    imported = import_artifact(
        database,
        build_canon_artifact(
            files, name=source_name, recorded_by=recorded_by, provided_by=provided_by
        ),
    )
    return {
        "corpus": registration,
        "import": imported.as_json(),
        "summary": canon_summary(files),
    }


def canon_summary(files: Sequence[CanonFile]) -> dict[str, Any]:
    by_fidelity: dict[str, int] = {}
    for item in files:
        by_fidelity[item.fidelity] = by_fidelity.get(item.fidelity, 0) + 1
    return {
        "files": len(files),
        "bytes": sum(item.bytes for item in files),
        "corpusSha256": canon_corpus_sha256(files),
        "byFidelity": dict(sorted(by_fidelity.items())),
        "quotable": sum(1 for item in files if item.quotable),
        "blockedFromQuotation": sorted(item.relative_path for item in files if not item.quotable),
        "documents": [item.as_json() for item in files],
    }
