"""Frozen Radar V2 application compatibility manifest validation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast

from packages.contracts.json_types import JsonObject
from packages.storage.sqlite_profile import REQUIRED_SQLITE_PROFILE

CONTRACT_VERSION: Final = "1.0.0"
ARTIFACT_KINDS: Final = ("api", "migration-bundle", "web")
_ID: Final = re.compile(r"^[a-z0-9][a-z0-9._:-]{7,127}$")
_SHA256: Final = re.compile(r"^[a-f0-9]{64}$")
_GIT_COMMIT: Final = re.compile(r"^[a-f0-9]{40}$")
_TIMESTAMP: Final = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$"
)


class ApplicationManifestError(ValueError):
    """An application compatibility manifest violates contract v1."""


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    """One immutable role artifact bound by name, size and SHA-256."""

    name: str
    kind: str
    sha256: str
    bytes: int


@dataclass(frozen=True, slots=True)
class ApplicationManifest:
    """Validated compatibility and provenance identity for one application release."""

    application_release_id: str
    git_commit: str
    created_at: str
    schema_version: int
    sqlite_version: str
    sqlite_source_id: str
    sqlite_compile_options: tuple[str, ...]
    artifacts: tuple[ArtifactDescriptor, ...]
    document: JsonObject


def canonical_json_bytes(document: Mapping[str, object]) -> bytes:
    """Return stable UTF-8 JSON bytes with one trailing newline."""
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _exact(document: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(document, dict) or any(not isinstance(key, str) for key in document):
        raise ApplicationManifestError(f"{label} must be an object with string keys")
    result = cast(dict[str, object], document)
    if set(result) != keys:
        raise ApplicationManifestError(f"{label} has unknown or missing fields")
    return result


def validate_utc_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ApplicationManifestError(f"{label} must be second-precision UTC")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise ApplicationManifestError(f"{label} is not a real UTC instant") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ApplicationManifestError(f"{label} is not canonical UTC")
    return value


def _constant_versions(document: dict[str, object]) -> None:
    constants: dict[str, object] = {
        "candidateContractVersions": [CONTRACT_VERSION],
        "contractVersion": CONTRACT_VERSION,
        "deltaContractVersions": [CONTRACT_VERSION],
        "gazetteContractVersions": [CONTRACT_VERSION],
        "manifestKind": "application",
        "publicApiVersion": CONTRACT_VERSION,
        "resultContractVersions": [CONTRACT_VERSION],
        "schemaVersion": REQUIRED_SQLITE_PROFILE.user_version,
        "tableContractVersion": CONTRACT_VERSION,
    }
    for field, expected in constants.items():
        if document[field] != expected:
            raise ApplicationManifestError(f"application manifest {field} differs from v1")


def _artifact(value: object) -> ArtifactDescriptor:
    document = _exact(value, {"bytes", "kind", "name", "sha256"}, "artifact descriptor")
    name = document["name"]
    kind = document["kind"]
    digest = document["sha256"]
    byte_count = document["bytes"]
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 128
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
    ):
        raise ApplicationManifestError("artifact name is not a simple bounded filename")
    if not isinstance(kind, str) or kind not in ARTIFACT_KINDS:
        raise ApplicationManifestError("artifact kind is not allowed")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ApplicationManifestError("artifact SHA-256 is invalid")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 1:
        raise ApplicationManifestError("artifact byte count is invalid")
    return ArtifactDescriptor(name=name, kind=kind, sha256=digest, bytes=byte_count)


def validate_application_manifest(document: Mapping[str, object]) -> ApplicationManifest:
    """Validate the stricter Stage 9 application subset of compatibility-manifest v1."""
    root = _exact(
        dict(document),
        {
            "applicationReleaseId",
            "artifacts",
            "candidateContractVersions",
            "contractVersion",
            "createdAt",
            "deltaContractVersions",
            "gazetteContractVersions",
            "gitCommit",
            "manifestKind",
            "publicApiVersion",
            "resultContractVersions",
            "schemaVersion",
            "sqliteRuntime",
            "tableContractVersion",
        },
        "application manifest",
    )
    _constant_versions(root)
    release_id = root["applicationReleaseId"]
    git_commit = root["gitCommit"]
    if not isinstance(release_id, str) or not _ID.fullmatch(release_id):
        raise ApplicationManifestError("applicationReleaseId is invalid")
    if not isinstance(git_commit, str) or not _GIT_COMMIT.fullmatch(git_commit):
        raise ApplicationManifestError("gitCommit is invalid")
    created_at = validate_utc_timestamp(root["createdAt"], "createdAt")

    raw_artifacts = root["artifacts"]
    if not isinstance(raw_artifacts, list):
        raise ApplicationManifestError("artifacts must be an array")
    artifacts = tuple(_artifact(value) for value in raw_artifacts)
    if len(artifacts) != 3:
        raise ApplicationManifestError("Stage 9 requires exactly api, web and migration artifacts")
    if tuple(sorted(item.kind for item in artifacts)) != ARTIFACT_KINDS:
        raise ApplicationManifestError("application artifact kinds are incomplete or duplicated")
    if len({item.name for item in artifacts}) != len(artifacts):
        raise ApplicationManifestError("application artifact names are duplicated")

    runtime = _exact(
        root["sqliteRuntime"],
        {"compileOptions", "sourceId", "version"},
        "sqliteRuntime",
    )
    version = runtime["version"]
    source_id = runtime["sourceId"]
    compile_options = runtime["compileOptions"]
    if version != REQUIRED_SQLITE_PROFILE.version:
        raise ApplicationManifestError("SQLite version differs from the accepted runtime")
    if source_id != REQUIRED_SQLITE_PROFILE.source_id:
        raise ApplicationManifestError("SQLite source id differs from the accepted runtime")
    if (
        not isinstance(compile_options, list)
        or any(not isinstance(item, str) or not item for item in compile_options)
        or len(set(compile_options)) != len(compile_options)
    ):
        raise ApplicationManifestError("SQLite compileOptions are invalid")
    options = tuple(cast(list[str], compile_options))
    if tuple(sorted(options)) != options:
        raise ApplicationManifestError("SQLite compileOptions must be sorted")
    if not REQUIRED_SQLITE_PROFILE.compile_options <= set(options):
        raise ApplicationManifestError("SQLite compileOptions omit a required option")

    return ApplicationManifest(
        application_release_id=release_id,
        git_commit=git_commit,
        created_at=created_at,
        schema_version=cast(int, root["schemaVersion"]),
        sqlite_version=cast(str, version),
        sqlite_source_id=cast(str, source_id),
        sqlite_compile_options=options,
        artifacts=artifacts,
        document=cast(JsonObject, root),
    )


def parse_application_manifest(content: bytes) -> ApplicationManifest:
    """Parse canonical manifest bytes and reject alternate byte encodings."""
    try:
        parsed = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ApplicationManifestError(f"application manifest JSON is invalid: {error}") from error
    if not isinstance(parsed, dict):
        raise ApplicationManifestError("application manifest root is not an object")
    document = cast(dict[str, object], parsed)
    if canonical_json_bytes(document) != content:
        raise ApplicationManifestError("application manifest is not canonical JSON")
    return validate_application_manifest(document)


__all__ = [
    "ARTIFACT_KINDS",
    "CONTRACT_VERSION",
    "ApplicationManifest",
    "ApplicationManifestError",
    "ArtifactDescriptor",
    "canonical_json_bytes",
    "parse_application_manifest",
    "validate_application_manifest",
    "validate_utc_timestamp",
]
