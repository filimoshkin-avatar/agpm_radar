from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PARSER_NAME = "radar-kx"
PARSER_VERSION = "canonical-v4"
PARSER_CONFIG_HASH = hashlib.sha256(
    b"radar-kx:canonical-v4:nfc:lf:nul-replacement:trim-lines:max-two-blank-lines"
    b":structured-article-json"
).hexdigest()


@dataclass(frozen=True, slots=True)
class TextChunk:
    ordinal: int
    char_start: int
    char_end: int
    text: str
    text_sha256: str
    chunk_id: str


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def stable_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonicalize_text(value: str) -> str:
    normalized = (
        unicodedata.normalize("NFC", value)
        .replace("\x00", "\ufffd")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    lines = [" ".join(line.split()) for line in normalized.split("\n")]
    output: list[str] = []
    blank_count = 0
    for line in lines:
        if line:
            blank_count = 0
            output.append(line)
            continue
        blank_count += 1
        if output and blank_count <= 2:
            output.append("")
    return "\n".join(output).strip()


def document_id(canonical_url: str) -> str:
    return sha256_bytes(canonical_url.encode("utf-8"))


def version_id(
    *,
    document: str,
    raw_sha256: str,
    text_sha256: str,
    parser_config_sha256: str = PARSER_CONFIG_HASH,
) -> str:
    payload = "\0".join((document, raw_sha256, parser_config_sha256, text_sha256))
    return sha256_bytes(payload.encode("ascii"))


def chunk_text(version: str, text: str, *, max_chars: int = 4000) -> tuple[TextChunk, ...]:
    if max_chars < 500:
        raise ValueError("max_chars must be at least 500")
    chunks: list[TextChunk] = []
    start = 0
    ordinal = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            lower_bound = start + max_chars // 2
            paragraph_break = text.rfind("\n\n", lower_bound, end)
            line_break = text.rfind("\n", lower_bound, end)
            word_break = text.rfind(" ", lower_bound, end)
            boundary = max(paragraph_break + 2, line_break + 1, word_break + 1)
            if boundary > lower_bound:
                end = boundary
        value = text[start:end]
        digest = sha256_bytes(value.encode("utf-8"))
        identifier = sha256_bytes(f"{version}\0{ordinal}\0{start}\0{end}\0{digest}".encode("ascii"))
        chunks.append(
            TextChunk(
                ordinal=ordinal,
                char_start=start,
                char_end=end,
                text=value,
                text_sha256=digest,
                chunk_id=identifier,
            )
        )
        start = end
        ordinal += 1
    return tuple(chunks)
