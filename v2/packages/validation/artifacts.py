"""Fail-closed validation for deterministic Radar V2 JSON and DOCX artifacts."""

from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from packages.domain.snapshot import JsonObject
from packages.renderers.daily_json import parse_public_issue_json
from packages.validation.public_issue import validate_public_issue_document

_W: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_FIXED_ZIP_TIME: Final = (1980, 1, 1, 0, 0, 0)
_DOCX_MEMBERS: Final = (
    "[Content_Types].xml",
    "_rels/.rels",
    "docProps/app.xml",
    "docProps/core.xml",
    "word/_rels/document.xml.rels",
    "word/document.xml",
    "word/styles.xml",
)
_FORBIDDEN_WORD_TAGS: Final = frozenset(
    {
        "altChunk",
        "control",
        "fldSimple",
        "instrText",
        "object",
        "subDoc",
    }
)


class ArtifactValidationError(ValueError):
    """A rendered artifact is malformed, unsafe or not deterministic."""


@dataclass(frozen=True, slots=True)
class DocxValidationReport:
    """Inspectable structure evidence for one accepted DOCX."""

    member_count: int
    paragraph_count: int
    hyperlink_count: int
    text_sha256: str
    text: str


def _xml(content: bytes, label: str) -> ET.Element:
    lowered = content.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ArtifactValidationError(f"DTD/entities are forbidden in {label}")
    try:
        # Bounded package members are preflighted above; runtime remains dependency-free.
        return ET.fromstring(content)  # noqa: S314
    except ET.ParseError as error:
        raise ArtifactValidationError(f"invalid XML in {label}: {error}") from error


def validate_daily_json(content: bytes) -> JsonObject:
    """Validate canonical public JSON bytes against the runtime IssueDetail contract."""
    if not content or len(content) > 4 * 1024 * 1024:
        raise ArtifactValidationError("daily JSON size is outside 1..4 MiB")
    try:
        return parse_public_issue_json(content)
    except ValueError as error:
        raise ArtifactValidationError(str(error)) from error


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    if info.filename.startswith("/") or "\\" in info.filename:
        raise ArtifactValidationError(f"unsafe DOCX member path: {info.filename}")
    parts = info.filename.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArtifactValidationError(f"unsafe DOCX member path: {info.filename}")
    if info.flag_bits & 0x1:
        raise ArtifactValidationError(f"encrypted DOCX member: {info.filename}")
    mode = (info.external_attr >> 16) & 0xFFFF
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o644:
        raise ArtifactValidationError(f"non-normalized DOCX member mode: {info.filename}")
    if info.date_time != _FIXED_ZIP_TIME:
        raise ArtifactValidationError(f"non-deterministic DOCX member timestamp: {info.filename}")
    if info.file_size > 8 * 1024 * 1024 or info.compress_size > 8 * 1024 * 1024:
        raise ArtifactValidationError(f"oversized DOCX member: {info.filename}")


def _relationship_map(root: ET.Element) -> dict[str, tuple[str, str, str | None]]:
    records: dict[str, tuple[str, str, str | None]] = {}
    for relationship in root.findall(f"{{{_PR}}}Relationship"):
        relationship_id = relationship.get("Id")
        target = relationship.get("Target")
        relationship_type = relationship.get("Type")
        if not relationship_id or not target or not relationship_type or relationship_id in records:
            raise ArtifactValidationError("DOCX relationship is incomplete or duplicated")
        records[relationship_id] = (target, relationship_type, relationship.get("TargetMode"))
    return records


def _docx_text(document: ET.Element) -> tuple[str, int]:
    paragraphs: list[str] = []
    for paragraph in document.iter(f"{{{_W}}}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{{{_W}}}t"))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs), len(paragraphs)


def validate_daily_docx(
    content: bytes,
    *,
    expected_document: Mapping[str, object] | None = None,
) -> DocxValidationReport:
    """Validate exact OOXML membership, links, structure and expected semantic content."""
    if not content or len(content) > 16 * 1024 * 1024:
        raise ArtifactValidationError("DOCX size is outside 1..16 MiB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content), mode="r")
    except zipfile.BadZipFile as error:
        raise ArtifactValidationError("DOCX is not a ZIP package") from error
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if names != sorted(_DOCX_MEMBERS) or len(names) != len(set(names)):
            raise ArtifactValidationError("DOCX membership/order differs from renderer contract")
        for info in infos:
            _validate_zip_member(info)
        files = {name: archive.read(name) for name in names}

    for name, payload in files.items():
        if name.endswith(".xml") or name.endswith(".rels"):
            _xml(payload, name)
    content_types = _xml(files["[Content_Types].xml"], "[Content_Types].xml")
    overrides = {
        node.get("PartName")
        for node in content_types
        if node.tag.endswith("Override") and node.get("PartName")
    }
    if not {"/word/document.xml", "/word/styles.xml"}.issubset(overrides):
        raise ArtifactValidationError("DOCX content types omit required Word parts")

    root_relationships = _relationship_map(_xml(files["_rels/.rels"], "_rels/.rels"))
    office_documents = [
        record for record in root_relationships.values() if record[1].endswith("/officeDocument")
    ]
    if (
        len(office_documents) != 1
        or office_documents[0][0] != "word/document.xml"
        or office_documents[0][2] is not None
    ):
        raise ArtifactValidationError("DOCX root officeDocument relationship is invalid")

    document = _xml(files["word/document.xml"], "word/document.xml")
    for node in document.iter():
        local = node.tag.rpartition("}")[2]
        if local in _FORBIDDEN_WORD_TAGS:
            raise ArtifactValidationError(f"forbidden active Word element: {local}")
    document_relationships = _relationship_map(
        _xml(files["word/_rels/document.xml.rels"], "word/_rels/document.xml.rels")
    )
    external: dict[str, str] = {}
    for relationship_id, (target, relationship_type, target_mode) in document_relationships.items():
        if relationship_type.endswith("/styles"):
            if (target, target_mode) != ("styles.xml", None):
                raise ArtifactValidationError("DOCX styles relationship is invalid")
            continue
        if not relationship_type.endswith("/hyperlink") or target_mode != "External":
            raise ArtifactValidationError("DOCX contains a non-allowlisted document relationship")
        parsed = urlsplit(target)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ArtifactValidationError("DOCX hyperlink target is not safe HTTP(S)")
        external[relationship_id] = target

    hyperlink_ids = [node.get(f"{{{_R}}}id") for node in document.iter(f"{{{_W}}}hyperlink")]
    if any(relationship_id is None for relationship_id in hyperlink_ids):
        raise ArtifactValidationError("DOCX hyperlink has no relationship id")
    if len(hyperlink_ids) != len(set(hyperlink_ids)) or set(cast(list[str], hyperlink_ids)) != set(
        external
    ):
        raise ArtifactValidationError("DOCX hyperlink relationships are missing or duplicated")

    text, paragraph_count = _docx_text(document)
    if paragraph_count < 6 or not text:
        raise ArtifactValidationError("DOCX has insufficient document structure")
    if expected_document is not None:
        expected = validate_public_issue_document(dict(expected_document))
        required_text = [cast(str, expected["title"]), cast(str, expected["issueDate"])]
        for raw_material in cast(list[object], expected["materials"]):
            material = cast(dict[str, object], raw_material)
            required_text.extend((cast(str, material["title"]), cast(str, material["url"])))
        missing = [value for value in required_text if value not in text]
        if missing:
            raise ArtifactValidationError(f"DOCX omits expected semantic content: {missing!r}")
        expected_urls = {
            cast(str, cast(dict[str, object], material)["url"])
            for material in cast(list[object], expected["materials"])
        }
        if set(external.values()) != expected_urls:
            raise ArtifactValidationError("DOCX hyperlink set differs from public issue")
    return DocxValidationReport(
        member_count=len(files),
        paragraph_count=paragraph_count,
        hyperlink_count=len(external),
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
    )


__all__ = [
    "ArtifactValidationError",
    "DocxValidationReport",
    "validate_daily_docx",
    "validate_daily_json",
]
