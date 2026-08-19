"""Deterministic, dependency-free validation for immutable gazette HTML/assets."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Final, cast
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from packages.domain.candidates import CandidateValidationError, validate_candidate
from packages.storage.safe_files import SafeFilesystemError, relative_parts

_BANNED_TAGS: Final = frozenset({"applet", "base", "embed", "form", "iframe", "object", "script"})
_VOID_TAGS: Final = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_ACTIVE_CSS: Final = re.compile(
    r"(?i)(?:@import\b|expression\s*\(|javascript\s*:|behavior\s*:|-moz-binding\s*:)"
)
_CSS_URL: Final = re.compile(r"(?is)url\(\s*(['\"]?)(.*?)\1\s*\)")
_WRITE_CALL: Final = re.compile(
    r"(?i)(?:document\s*\.\s*write|XMLHttpRequest|WebSocket\s*\(|fetch\s*\()"
)
_INTERNAL_STATE_NAME: Final = ".open" + "claw"
_INTERNAL_DATA_PATH: Final = "data/" + "db"
_INTERNAL_REFERENCE: Final = re.compile(
    r"(?i)(?:/api/(?:internal|admin)|localhost|127\.0\.0\.1|\[::1\]|"
    + re.escape(_INTERNAL_DATA_PATH)
    + r"|"
    + re.escape(_INTERNAL_STATE_NAME)
    + r")"
)


class GazetteValidationError(ValueError):
    """A gazette package is malformed, unsafe or not self-contained."""


@dataclass(frozen=True, slots=True)
class GazetteValidationReport:
    """Inspectable acceptance evidence for one gazette candidate."""

    entrypoint: str
    asset_count: int
    local_reference_count: int
    external_link_count: int
    entrypoint_sha256: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Reference:
    source_path: str
    value: str
    external_allowed: bool


class _GazetteHtmlParser(HTMLParser):
    def __init__(self, source_path: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_path = source_path
        self.doctype = False
        self.counts: dict[str, int] = {"body": 0, "head": 0, "html": 0, "title": 0}
        self.end_counts: dict[str, int] = {"body": 0, "head": 0, "html": 0, "title": 0}
        self.stack: list[str] = []
        self.references: list[_Reference] = []
        self.title_parts: list[str] = []
        self.style_parts: list[str] = []

    def handle_decl(self, decl: str) -> None:
        if decl.strip().casefold() == "doctype html":
            self.doctype = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if normalized in _BANNED_TAGS:
            raise GazetteValidationError(f"forbidden HTML tag <{normalized}> in {self.source_path}")
        if normalized in self.counts:
            self.counts[normalized] += 1
        names = [name.casefold() for name, _value in attrs]
        if len(names) != len(set(names)):
            raise GazetteValidationError(f"duplicate HTML attribute in {self.source_path}")
        attributes = {name.casefold(): value or "" for name, value in attrs}
        for name, value in attributes.items():
            if name.startswith("on") or name in {"action", "formaction", "srcdoc"}:
                raise GazetteValidationError(
                    f"active HTML attribute {name!r} in {self.source_path}"
                )
            if name in {"data-draft", "data-internal", "data-service"}:
                raise GazetteValidationError(
                    f"internal HTML block marker {name!r} in {self.source_path}"
                )
            if _WRITE_CALL.search(value) or _INTERNAL_REFERENCE.search(value):
                raise GazetteValidationError(
                    f"active/internal HTML attribute value in {self.source_path}"
                )
            if name == "style" and _ACTIVE_CSS.search(value):
                raise GazetteValidationError(f"active inline CSS in {self.source_path}")
        if normalized == "meta" and attributes.get("http-equiv", "").casefold() == "refresh":
            raise GazetteValidationError(f"HTML refresh is forbidden in {self.source_path}")
        if normalized == "a" and attributes.get("target", "").casefold() == "_blank":
            rel = set(attributes.get("rel", "").casefold().split())
            if not {"noopener", "noreferrer"}.issubset(rel):
                raise GazetteValidationError(
                    f"target=_blank requires noopener noreferrer in {self.source_path}"
                )
        if "href" in attributes:
            self.references.append(
                _Reference(self.source_path, attributes["href"], normalized == "a")
            )
        if "src" in attributes:
            self.references.append(_Reference(self.source_path, attributes["src"], False))
        if "poster" in attributes:
            self.references.append(_Reference(self.source_path, attributes["poster"], False))
        if "srcset" in attributes:
            for candidate in attributes["srcset"].split(","):
                value = candidate.strip().split(maxsplit=1)[0]
                if value:
                    self.references.append(_Reference(self.source_path, value, False))
        if normalized not in _VOID_TAGS:
            self.stack.append(normalized)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        normalized = tag.casefold()
        if normalized not in _VOID_TAGS and self.stack and self.stack[-1] == normalized:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in self.end_counts:
            self.end_counts[normalized] += 1
        if not self.stack or self.stack[-1] != normalized:
            raise GazetteValidationError(
                f"unbalanced HTML end tag </{normalized}> in {self.source_path}"
            )
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        if _WRITE_CALL.search(data) or _INTERNAL_REFERENCE.search(data):
            raise GazetteValidationError(f"active/internal HTML text in {self.source_path}")
        if self.stack and self.stack[-1] == "title":
            self.title_parts.append(data)
        if "style" in self.stack:
            self.style_parts.append(data)

    def close_and_validate(self) -> None:
        try:
            super().close()
        except GazetteValidationError:
            raise
        if self.stack:
            raise GazetteValidationError(
                f"unclosed HTML tags in {self.source_path}: {self.stack!r}"
            )
        if not self.doctype:
            raise GazetteValidationError(f"HTML5 doctype is required in {self.source_path}")
        for tag in ("html", "head", "title", "body"):
            if self.counts[tag] != 1 or self.end_counts[tag] != 1:
                raise GazetteValidationError(
                    f"exactly one balanced <{tag}> is required in {self.source_path}"
                )
        style = "\n".join(self.style_parts)
        if _ACTIVE_CSS.search(style):
            raise GazetteValidationError(f"active embedded CSS in {self.source_path}")

    @property
    def title(self) -> str:
        return " ".join(" ".join(self.title_parts).split())


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _text(content: bytes, label: str) -> str:
    try:
        value = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GazetteValidationError(f"{label} must be UTF-8") from error
    if "\x00" in value:
        raise GazetteValidationError(f"{label} contains NUL")
    return value


def _resolve_local(source_path: str, reference: str) -> str | None:
    if not reference or reference.startswith("#"):
        return None
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc:
        return None
    decoded_path = unquote(parsed.path)
    if not decoded_path:
        return None
    if decoded_path.startswith("/") or "\\" in decoded_path:
        raise GazetteValidationError(f"gazette reference is not package-relative: {reference!r}")
    joined = PurePosixPath(source_path).parent.joinpath(decoded_path)
    normalized_parts: list[str] = []
    for part in joined.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not normalized_parts:
                raise GazetteValidationError(f"gazette reference escapes package: {reference!r}")
            normalized_parts.pop()
        else:
            normalized_parts.append(part)
    if not normalized_parts:
        raise GazetteValidationError(f"gazette reference has no asset path: {reference!r}")
    result = "/".join(normalized_parts)
    try:
        relative_parts(result)
    except SafeFilesystemError as error:
        raise GazetteValidationError(f"unsafe gazette reference: {reference!r}") from error
    return result


def _classify_reference(reference: _Reference) -> tuple[str | None, str | None]:
    value = reference.value.strip()
    if not value or value.startswith("#"):
        return None, None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        if not reference.external_allowed:
            raise GazetteValidationError(
                f"external asset dependency is forbidden in {reference.source_path}: {value!r}"
            )
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise GazetteValidationError(
                f"external link is not safe HTTP(S) in {reference.source_path}: {value!r}"
            )
        return None, value
    return _resolve_local(reference.source_path, value), None


def _css_references(path: str, content: bytes) -> tuple[_Reference, ...]:
    text = _text(content, path)
    if _ACTIVE_CSS.search(text) or _WRITE_CALL.search(text) or _INTERNAL_REFERENCE.search(text):
        raise GazetteValidationError(f"active/internal CSS in {path}")
    references: list[_Reference] = []
    for match in _CSS_URL.finditer(text):
        value = match.group(2).strip()
        if value and not value.startswith("#"):
            references.append(_Reference(path, value, False))
    return tuple(references)


def _validate_svg(path: str, content: bytes) -> tuple[_Reference, ...]:
    text = _text(content, path)
    lowered = text.casefold()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise GazetteValidationError(f"DTD/entities are forbidden in SVG: {path}")
    try:
        # Candidate size is bounded and DTD/entities are rejected above.
        root = ET.fromstring(text)  # noqa: S314
    except ET.ParseError as error:
        raise GazetteValidationError(f"invalid SVG XML in {path}") from error
    references: list[_Reference] = []
    for node in root.iter():
        local = node.tag.rpartition("}")[2].casefold()
        if local in {"foreignobject", "script"}:
            raise GazetteValidationError(f"active SVG element in {path}: {local}")
        for raw_name, value in node.attrib.items():
            name = raw_name.rpartition("}")[2].casefold()
            if name.startswith("on") or _ACTIVE_CSS.search(value) or _WRITE_CALL.search(value):
                raise GazetteValidationError(f"active SVG attribute in {path}: {name}")
            if name in {"href", "src"}:
                references.append(_Reference(path, value, False))
    return tuple(references)


def _validate_binary(media_type: str, path: str, content: bytes) -> None:
    valid = True
    if media_type == "image/png":
        valid = content.startswith(b"\x89PNG\r\n\x1a\n")
    elif media_type == "image/jpeg":
        valid = content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9")
    elif media_type == "image/webp":
        valid = len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    elif media_type == "font/woff2":
        valid = content.startswith(b"wOF2")
    elif media_type == "font/ttf":
        valid = content.startswith((b"\x00\x01\x00\x00", b"OTTO"))
    if not valid:
        raise GazetteValidationError(f"asset signature differs from {media_type}: {path}")


def validate_gazette_candidate(
    candidate: Mapping[str, object],
    assets: Mapping[str, bytes],
) -> GazetteValidationReport:
    """Validate a Stage 5 gazette candidate and exact self-contained asset bytes."""
    try:
        validated = validate_candidate(candidate)
    except CandidateValidationError as error:
        raise GazetteValidationError(f"invalid gazette candidate: {error}") from error
    if validated["operation"] != "gazette":
        raise GazetteValidationError("gazette validator received a non-gazette candidate")
    descriptors = cast(list[dict[str, object]], validated["inputAssets"])
    descriptor_map = {cast(str, item["relativePath"]): item for item in descriptors}
    if set(assets) != set(descriptor_map):
        raise GazetteValidationError("gazette assets differ from candidate manifest")
    if any(
        not isinstance(path, str) or not isinstance(content, bytes)
        for path, content in assets.items()
    ):
        raise GazetteValidationError("gazette assets must map relative paths to bytes")

    references: list[_Reference] = []
    html_parsers: dict[str, _GazetteHtmlParser] = {}
    for path in sorted(descriptor_map):
        try:
            relative_parts(path)
        except SafeFilesystemError as error:
            raise GazetteValidationError(f"unsafe gazette asset path: {path!r}") from error
        descriptor = descriptor_map[path]
        content = assets[path]
        if descriptor["bytes"] != len(content) or descriptor["sha256"] != _sha256(content):
            raise GazetteValidationError(f"gazette asset bytes/hash differ: {path}")
        media_type = cast(str, descriptor["mediaType"])
        if media_type == "text/html":
            parser = _GazetteHtmlParser(path)
            try:
                parser.feed(_text(content, path))
                parser.close_and_validate()
            except GazetteValidationError:
                raise
            except Exception as error:
                raise GazetteValidationError(f"HTML parse failed closed for {path}") from error
            references.extend(parser.references)
            html_parsers[path] = parser
        elif media_type == "text/css":
            references.extend(_css_references(path, content))
        elif media_type == "image/svg+xml":
            references.extend(_validate_svg(path, content))
        else:
            _validate_binary(media_type, path, content)

    entrypoint = cast(str, validated["htmlEntrypoint"])
    entry_parser = html_parsers.get(entrypoint)
    if entry_parser is None:
        raise GazetteValidationError("gazette htmlEntrypoint is missing or not text/html")
    if entry_parser.title != validated["title"]:
        raise GazetteValidationError("gazette HTML title differs from candidate title")

    local_references: set[tuple[str, str]] = set()
    external_links: set[str] = set()
    for reference in references:
        local, external = _classify_reference(reference)
        if local is not None:
            if local not in assets:
                raise GazetteValidationError(
                    "gazette local link target is absent: "
                    f"{reference.value!r} from {reference.source_path}"
                )
            local_references.add((reference.source_path, local))
        if external is not None:
            external_links.add(external)
    warnings = tuple(f"external-link:{value}" for value in sorted(external_links))
    return GazetteValidationReport(
        entrypoint=entrypoint,
        asset_count=len(assets),
        local_reference_count=len(local_references),
        external_link_count=len(external_links),
        entrypoint_sha256=_sha256(assets[entrypoint]),
        warnings=warnings,
    )


__all__ = [
    "GazetteValidationError",
    "GazetteValidationReport",
    "validate_gazette_candidate",
]
