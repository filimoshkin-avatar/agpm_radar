"""Import documents that arrived as files rather than as HTTP responses.

Rung seven of the acquisition ladder, and the only tool that can load the AgPM
canon: material that is already on this host, either handed over by an operator
or sitting in a local directory. No network request is made and none is
recorded - a synthetic ``fetch_attempts`` row with a fabricated HTTP 200 is
exactly the mistake that produced defect D9, where two ordinary browser-header
fetches ended up in the evidence base labelled as operator artifacts.

What arrives instead is provenance, and it is mandatory. A manifest entry
without it is refused, because a document whose origin nobody recorded cannot be
quoted publicly and there is no later moment at which the origin becomes
knowable.

The manifest is JSON::

    {
      "artifact": {
        "name": "operator-html-artifact-20260822",
        "source_kind": "operator_artifact",
        "recorded_by": "ivan",
        "provided_by": "ivan",
        "provided_at": "2026-08-22T06:00:00Z"
      },
      "documents": [
        {
          "canonical_url": "https://adopt.ai/blog/enterprise-ai-agents",
          "path": "files/adopt-ai.html",
          "content_type": "text/html; charset=utf-8",
          "provenance": {
            "source_access_method": "web_archive",
            "archive_used": true,
            "manual_review_required": true,
            "manual_review_reason": "snapshot URL and capture date were not recorded"
          }
        }
      ]
    }

``path`` resolves against the manifest's own directory and may not escape it.
Everything under ``artifact`` supplies the default for the corresponding field of
each entry.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radar_kx.database import ARTIFACT_SOURCE_KINDS, Database, VersionProvenance
from radar_kx.parser import parse_content
from radar_kx.url_policy import canonical_identity_url

#: Provenance shapes an offline import may legitimately claim. ``http_default``,
#: ``browser_headers``, ``robots_override`` and ``browser_render`` describe a
#: request this process did not make, so a file may not claim them.
ARTIFACT_ACCESS_METHODS = frozenset({"web_archive", "operator_file", "local_import"})


class ArtifactManifestError(ValueError):
    """The manifest describes an import that must not happen."""


@dataclass(frozen=True, slots=True)
class ProvenanceCorrection:
    """Provenance to append to one document's versions.

    ``source_kinds`` narrows the correction to the versions it is actually about.
    ``None`` means every version of the document, which is right only when the
    document has one.
    """

    canonical_url: str
    provenance: VersionProvenance
    source_kinds: frozenset[str] | None


@dataclass(frozen=True, slots=True)
class ArtifactDocument:
    canonical_url: str
    path: Path
    content_type: str
    source_kind: str
    fetched_at: datetime
    provenance: VersionProvenance


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    name: str
    recorded_by: str
    documents: tuple[ArtifactDocument, ...]


@dataclass(frozen=True, slots=True)
class ArtifactImportResult:
    manifest: str
    documents: int
    versions_created: int
    versions_already_present: int
    complete_versions: int
    incomplete_versions: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest,
            "documents": self.documents,
            "versionsCreated": self.versions_created,
            "versionsAlreadyPresent": self.versions_already_present,
            "completeVersions": self.complete_versions,
            "incompleteVersions": list(self.incomplete_versions),
        }


def _object(value: object, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ArtifactManifestError(f"{where} must be an object")
    return value


def _text(value: object, *, where: str, required: bool = True) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ArtifactManifestError(f"{where} is required")
        return None
    if not isinstance(value, str):
        raise ArtifactManifestError(f"{where} must be a string")
    return value.strip()


def _timestamp(value: object, *, where: str, required: bool = True) -> datetime | None:
    raw = _text(value, where=where, required=required)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ArtifactManifestError(f"{where} is not an ISO-8601 timestamp: {raw!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _flag(value: object, *, where: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ArtifactManifestError(f"{where} must be true or false")
    return value


def _source_kinds(value: object, *, where: str) -> frozenset[str] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ArtifactManifestError(f"{where}.source_kinds must be a non-empty array of strings")
    return frozenset(value)


def _resolve_path(root: Path, raw: str, *, where: str) -> Path:
    candidate = (root / raw).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ArtifactManifestError(f"{where} escapes the manifest directory: {raw!r}")
    if not candidate.is_file():
        raise ArtifactManifestError(f"{where} does not exist: {raw!r}")
    return candidate


def _provenance(
    value: object,
    *,
    where: str,
    defaults: Mapping[str, Any],
) -> VersionProvenance:
    if value is None:
        raise ArtifactManifestError(
            f"{where} has no provenance; a file with no recorded origin cannot be imported"
        )
    payload = _object(value, where=where)
    method = _text(payload.get("source_access_method"), where=f"{where}.source_access_method")
    if method not in ARTIFACT_ACCESS_METHODS:
        raise ArtifactManifestError(
            f"{where}.source_access_method must be one of "
            f"{sorted(ARTIFACT_ACCESS_METHODS)}, got {method!r}"
        )
    archive_used = _flag(
        payload.get("archive_used"), where=f"{where}.archive_used", default=method == "web_archive"
    )
    provenance = VersionProvenance(
        source_access_method=method,
        archive_used=archive_used,
        archive_url=_text(payload.get("archive_url"), where=f"{where}.archive_url", required=False),
        archive_captured_at=_timestamp(
            payload.get("archive_captured_at"),
            where=f"{where}.archive_captured_at",
            required=False,
        ),
        browser_used=_flag(payload.get("browser_used"), where=f"{where}.browser_used"),
        manual_review_required=_flag(
            payload.get("manual_review_required"), where=f"{where}.manual_review_required"
        ),
        manual_review_reason=_text(
            payload.get("manual_review_reason"),
            where=f"{where}.manual_review_reason",
            required=False,
        ),
        provided_by=_text(
            payload.get("provided_by") or defaults.get("provided_by"),
            where=f"{where}.provided_by",
            required=False,
        ),
        provided_at=_timestamp(
            payload.get("provided_at") or defaults.get("provided_at"),
            where=f"{where}.provided_at",
            required=False,
        ),
        original_url=_text(
            payload.get("original_url"), where=f"{where}.original_url", required=False
        ),
        notes=_text(
            payload.get("notes") or defaults.get("notes"), where=f"{where}.notes", required=False
        ),
    )
    _reject_incoherent_provenance(provenance, where=where)
    return provenance


def _reject_incoherent_provenance(provenance: VersionProvenance, *, where: str) -> None:
    """Refuse in Python what the database would refuse in SQL, with a better message."""
    if provenance.source_access_method == "web_archive" and not provenance.archive_used:
        raise ArtifactManifestError(f"{where}: a web-archive import must set archive_used")
    if (
        provenance.archive_used
        and not provenance.manual_review_required
        and (provenance.archive_url is None or provenance.archive_captured_at is None)
    ):
        raise ArtifactManifestError(
            f"{where}: an archive snapshot needs archive_url and archive_captured_at, "
            "or manual_review_required with a reason"
        )
    if not provenance.archive_used and (
        provenance.archive_url is not None or provenance.archive_captured_at is not None
    ):
        raise ArtifactManifestError(f"{where}: archive fields set without archive_used")
    if provenance.manual_review_required and provenance.manual_review_reason is None:
        raise ArtifactManifestError(f"{where}: manual_review_required needs a reason")
    if provenance.source_access_method in {"operator_file", "local_import"} and (
        provenance.provided_by is None or provenance.provided_at is None
    ):
        raise ArtifactManifestError(
            f"{where}: material that reached us by hand needs provided_by and provided_at"
        )


def load_artifact_manifest(path: Path) -> ArtifactManifest:
    root = path.parent
    document = _object(json.loads(path.read_text(encoding="utf-8")), where="manifest")
    artifact = _object(document.get("artifact"), where="manifest.artifact")
    name = _text(artifact.get("name"), where="manifest.artifact.name")
    recorded_by = _text(artifact.get("recorded_by"), where="manifest.artifact.recorded_by")
    default_kind = _text(artifact.get("source_kind"), where="manifest.artifact.source_kind")
    default_content_type = _text(
        artifact.get("content_type"), where="manifest.artifact.content_type", required=False
    )
    defaults = {
        "provided_by": artifact.get("provided_by"),
        "provided_at": artifact.get("provided_at"),
        "notes": artifact.get("notes"),
    }
    entries = document.get("documents")
    if not isinstance(entries, list) or not entries:
        raise ArtifactManifestError("manifest.documents must be a non-empty array")

    documents: list[ArtifactDocument] = []
    seen: set[str] = set()
    for index, raw in enumerate(entries):
        where = f"manifest.documents[{index}]"
        entry = _object(raw, where=where)
        canonical_url = canonical_identity_url(
            str(_text(entry.get("canonical_url"), where=f"{where}.canonical_url"))
        )
        if canonical_url in seen:
            raise ArtifactManifestError(f"{where}: duplicate canonical URL {canonical_url}")
        seen.add(canonical_url)
        source_kind = _text(entry.get("source_kind") or default_kind, where=f"{where}.source_kind")
        if source_kind not in ARTIFACT_SOURCE_KINDS:
            raise ArtifactManifestError(
                f"{where}.source_kind must be one of {sorted(ARTIFACT_SOURCE_KINDS)}, "
                f"got {source_kind!r}"
            )
        content_type = _text(
            entry.get("content_type") or default_content_type,
            where=f"{where}.content_type",
            required=False,
        )
        path_value = str(_text(entry.get("path"), where=f"{where}.path"))
        resolved = _resolve_path(root, path_value, where=f"{where}.path")
        fetched_at = _timestamp(
            entry.get("fetched_at") or artifact.get("provided_at"),
            where=f"{where}.fetched_at",
            required=False,
        )
        documents.append(
            ArtifactDocument(
                canonical_url=canonical_url,
                path=resolved,
                content_type=content_type or _guess_content_type(resolved),
                source_kind=str(source_kind),
                fetched_at=fetched_at or datetime.fromtimestamp(resolved.stat().st_mtime, tz=UTC),
                provenance=_provenance(
                    entry.get("provenance"), where=f"{where}.provenance", defaults=defaults
                ),
            )
        )
    return ArtifactManifest(
        name=str(name), recorded_by=str(recorded_by), documents=tuple(documents)
    )


def _guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        return "text/html; charset=utf-8"
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".json":
        return "application/json"
    return "text/plain; charset=utf-8"


def import_artifact(
    database: Database,
    manifest: ArtifactManifest,
    *,
    min_text_chars: int | None = None,
) -> ArtifactImportResult:
    """Store every document the manifest names, with its provenance, in one pass."""
    threshold = min_text_chars if min_text_chars is not None else database.settings.min_text_chars
    created = 0
    already = 0
    complete = 0
    incomplete: list[str] = []
    for document in manifest.documents:
        body = document.path.read_bytes()
        parsed = parse_content(
            body=body,
            content_type=document.content_type,
            source_url=document.canonical_url,
            min_text_chars=threshold,
        )
        if not parsed.text.strip():
            raise ArtifactManifestError(
                f"{document.path.name}: parsed to no text at all "
                f"(quality={parsed.quality}); metadata is not full text"
            )
        outcome = database.store_artifact_version(
            canonical_url=document.canonical_url,
            body=body,
            parsed=parsed,
            source_kind=document.source_kind,
            fetched_at=document.fetched_at,
            provenance=document.provenance,
            recorded_by=manifest.recorded_by,
        )
        if outcome.created:
            created += 1
        else:
            already += 1
        if parsed.is_complete:
            complete += 1
        else:
            incomplete.append(document.canonical_url)
    return ArtifactImportResult(
        manifest=manifest.name,
        documents=len(manifest.documents),
        versions_created=created,
        versions_already_present=already,
        complete_versions=complete,
        incomplete_versions=tuple(incomplete),
    )


def load_provenance_corrections(path: Path) -> tuple[str, tuple[ProvenanceCorrection, ...]]:
    """Read a provenance-correction file.

    An entry names a document and, optionally, which of its versions the
    correction is about. A document usually has several versions - earlier
    partial captures, a legacy snapshot, the complete one - and they were not all
    obtained the same way, so a correction that does not say which versions it
    means writes something false onto the others.
    """
    document = _object(json.loads(path.read_text(encoding="utf-8")), where="corrections")
    recorded_by = _text(document.get("recorded_by"), where="corrections.recorded_by")
    entries = document.get("corrections")
    if not isinstance(entries, list) or not entries:
        raise ArtifactManifestError("corrections.corrections must be a non-empty array")
    corrections: list[ProvenanceCorrection] = []
    for index, raw in enumerate(entries):
        where = f"corrections[{index}]"
        entry = _object(raw, where=where)
        canonical_url = canonical_identity_url(
            str(_text(entry.get("canonical_url"), where=f"{where}.canonical_url"))
        )
        source_kinds = _source_kinds(entry.get("source_kinds"), where=where)
        payload = _object(entry.get("provenance"), where=f"{where}.provenance")
        method = _text(payload.get("source_access_method"), where=f"{where}.source_access_method")
        provenance = VersionProvenance(
            source_access_method=str(method),
            archive_used=_flag(
                payload.get("archive_used"),
                where=f"{where}.archive_used",
                default=method == "web_archive",
            ),
            archive_url=_text(
                payload.get("archive_url"), where=f"{where}.archive_url", required=False
            ),
            archive_captured_at=_timestamp(
                payload.get("archive_captured_at"),
                where=f"{where}.archive_captured_at",
                required=False,
            ),
            browser_used=_flag(payload.get("browser_used"), where=f"{where}.browser_used"),
            manual_review_required=_flag(
                payload.get("manual_review_required"), where=f"{where}.manual_review_required"
            ),
            manual_review_reason=_text(
                payload.get("manual_review_reason"),
                where=f"{where}.manual_review_reason",
                required=False,
            ),
            provided_by=_text(
                payload.get("provided_by"), where=f"{where}.provided_by", required=False
            ),
            provided_at=_timestamp(
                payload.get("provided_at"), where=f"{where}.provided_at", required=False
            ),
            original_url=_text(
                payload.get("original_url"), where=f"{where}.original_url", required=False
            ),
            notes=_text(payload.get("notes"), where=f"{where}.notes", required=False),
        )
        _reject_incoherent_provenance(provenance, where=where)
        corrections.append(
            ProvenanceCorrection(
                canonical_url=canonical_url,
                provenance=provenance,
                source_kinds=source_kinds,
            )
        )
    return str(recorded_by), tuple(corrections)


def record_provenance_corrections(
    database: Database,
    *,
    recorded_by: str,
    corrections: Sequence[ProvenanceCorrection],
) -> dict[str, Any]:
    """Append provenance for existing versions, skipping any that already say the same."""
    appended = 0
    unchanged = 0
    missing: list[str] = []
    for correction in corrections:
        outcome = database.record_version_provenance(
            canonical_url=correction.canonical_url,
            provenance=correction.provenance,
            recorded_by=recorded_by,
            source_kinds=correction.source_kinds,
        )
        if outcome is None:
            missing.append(correction.canonical_url)
            continue
        appended += outcome.appended
        unchanged += outcome.unchanged
    return {
        "appended": appended,
        "unchanged": unchanged,
        "documentsNotInStore": missing,
    }
