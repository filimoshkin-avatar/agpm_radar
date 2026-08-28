"""Deterministic role artifacts and the immutable Stage 9 application package."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from packages.contracts.json_types import JsonObject
from packages.deployment.manifest import (
    ApplicationManifest,
    canonical_json_bytes,
    parse_application_manifest,
)
from packages.storage.safe_files import SafeFilesystemError, relative_parts
from packages.storage.sqlite_profile import inspect_sqlite_runtime

APPLICATION_PACKAGE_NAME: Final = "radar-v2-application-release.tar.gz"
APPLICATION_PACKAGE_PREFIX: Final = "radar-v2-application-release"
COMPATIBILITY_MANIFEST_NAME: Final = "compatibility-manifest.json"
PROVENANCE_NAME: Final = "provenance.json"
CHECKSUMS_NAME: Final = "checksums.sha256"
ROLE_MANIFEST_NAME: Final = "MANIFEST.json"
_MAX_FILE_BYTES: Final = 64 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS: Final = 512
_FORBIDDEN_SUFFIXES: Final = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
)
_FORBIDDEN_CONTENT: Final = (
    b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----",
    b"-----BEGIN " + b"PRIVATE KEY-----",
    b"." + b"openclaw",
)


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """One role-specific allowlist and normalized archive identity."""

    kind: str
    name: str
    prefix: str
    paths: tuple[str, ...]
    globs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BuiltRoleArtifact:
    """One validated deterministic role archive."""

    spec: ArtifactSpec
    content: bytes
    manifest: bytes
    sha256: str
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApplicationProvenance:
    """Git and selected-source identity carried outside the compatibility schema."""

    application_release_id: str
    git_commit: str
    git_tag: str | None
    created_at: str
    source_tree_sha256: str
    document: JsonObject


@dataclass(frozen=True, slots=True)
class BuiltApplicationRelease:
    """Complete immutable package plus parsed evidence needed by the deployer."""

    content: bytes
    sha256: str
    manifest: ApplicationManifest
    manifest_bytes: bytes
    provenance: ApplicationProvenance
    provenance_bytes: bytes
    artifacts: tuple[BuiltRoleArtifact, ...]


@dataclass(frozen=True, slots=True)
class ParsedApplicationRelease:
    """Verified outer package and exact extracted role archives."""

    sha256: str
    manifest: ApplicationManifest
    manifest_bytes: bytes
    provenance: ApplicationProvenance
    provenance_bytes: bytes
    artifacts: dict[str, bytes]


API_PATHS: Final = (
    "apps/__init__.py",
    "apps/api/__init__.py",
    "apps/api/__main__.py",
    "apps/api/application.py",
    "apps/api/database.py",
    "apps/api/http_server.py",
    "apps/api/public_data.py",
    "apps/api/service.py",
    "packages/__init__.py",
    "packages/contracts/__init__.py",
    "packages/contracts/analysis.py",
    "packages/contracts/json_types.py",
    "packages/storage/__init__.py",
    "packages/storage/content_pointer.py",
    "packages/storage/hashing.py",
    "packages/storage/safe_files.py",
    "packages/storage/sqlite_profile.py",
    "packages/validation/__init__.py",
    "packages/validation/public_issue.py",
)
WEB_PATHS: Final = (
    "apps/web/app.mjs",
    "apps/web/favicon.svg",
    "apps/web/fonts/GolosText[wght].ttf",
    "apps/web/fonts/PTMono-Regular.ttf",
    # Every gazette issue is named here one by one, and that already cost a
    # release: the September issue sat in apps/web and was linked from
    # index.html, and the web role shipped without it because this list did not
    # know about it. The other place that moves with this one is
    # _BUNDLED_GAZETTE_ISSUES in apps/api/application.py. Caddy is no longer a
    # third: its matcher takes /gazette-*.html now.
    "apps/web/gazette-20260803.html",
    "apps/web/gazette-20260803-r2.html",
    "apps/web/gazette-20260901.html",
    "apps/web/gazette-20260901-r2.html",
    "apps/web/gazette-20260901-r3.html",
    "apps/web/index.html",
    "apps/web/og-image-20260803.png",
    "apps/web/styles.css",
    # The one vendored frontend dependency (provenance in apps/web/vendor/README.md).
    "apps/web/vendor/cytoscape.3.30.4.min.js",
)
MIGRATION_PATHS: Final = (
    "apps/__init__.py",
    "apps/migration_runner/__init__.py",
    "apps/migration_runner/__main__.py",
    "deploy/templates/Caddyfile.radar-v2",
    "deploy/templates/radar-v2-api.service",
    "packages/__init__.py",
    "packages/contracts/__init__.py",
    "packages/contracts/analysis.py",
    "packages/contracts/json_types.py",
    "packages/deployment/__init__.py",
    "packages/deployment/manifest.py",
    "packages/deployment/migration.py",
    "packages/storage/__init__.py",
    "packages/storage/hashing.py",
    "packages/storage/migrations.py",
    "packages/storage/mutation_lock.py",
    "packages/storage/safe_files.py",
    "packages/storage/sqlite_profile.py",
    "packages/validation/__init__.py",
    "packages/validation/public_issue.py",
)

API_ARTIFACT: Final = ArtifactSpec(
    kind="api",
    name="radar-v2-api.tar.gz",
    prefix="radar-v2-api",
    paths=API_PATHS,
)
WEB_ARTIFACT: Final = ArtifactSpec(
    kind="web",
    name="radar-v2-web.tar.gz",
    prefix="radar-v2-web",
    paths=WEB_PATHS,
)
MIGRATION_ARTIFACT: Final = ArtifactSpec(
    kind="migration-bundle",
    name="radar-v2-migrations.tar.gz",
    prefix="radar-v2-migrations",
    paths=MIGRATION_PATHS,
    globs=("packages/storage/migrations/*.sql",),
)
APPLICATION_ARTIFACTS: Final = (API_ARTIFACT, MIGRATION_ARTIFACT, WEB_ARTIFACT)
PUBLIC_PRODUCTION_ARTIFACT: Final = ArtifactSpec(
    kind="public-runtime",
    name="radar-v2-production.tar.gz",
    prefix="radar-v2-production",
    paths=tuple(sorted(set(API_PATHS + WEB_PATHS))),
)


class ApplicationArtifactError(RuntimeError):
    """A release source, archive or outer package is unsafe or inconsistent."""


def sha256_bytes(content: bytes) -> str:
    """Return lowercase SHA-256 for immutable package evidence."""
    return hashlib.sha256(content).hexdigest()


def _read_source_file(source_root: Path, relative: str) -> bytes:
    try:
        parts = relative_parts(relative)
    except SafeFilesystemError as error:
        raise ApplicationArtifactError(f"artifact source path is invalid: {relative}") from error
    path = source_root.joinpath(*parts)
    try:
        relative_to_source = path.relative_to(source_root)
    except ValueError as error:
        raise ApplicationArtifactError(f"artifact source escapes root: {relative}") from error
    if relative_to_source.as_posix() != relative:
        raise ApplicationArtifactError(f"artifact source is not canonical: {relative}")
    if not path.is_file() or path.is_symlink():
        raise ApplicationArtifactError(
            f"artifact source is not a regular no-symlink file: {relative}"
        )
    if path.name.lower().endswith(_FORBIDDEN_SUFFIXES):
        raise ApplicationArtifactError(f"database file selected for artifact: {relative}")
    content = path.read_bytes()
    if len(content) > _MAX_FILE_BYTES:
        raise ApplicationArtifactError(f"artifact source exceeds 64 MiB: {relative}")
    lowered = content.lower()
    for forbidden in _FORBIDDEN_CONTENT:
        if forbidden.lower() in lowered:
            raise ApplicationArtifactError(f"secret/OpenClaw content selected: {relative}")
    return content


def collect_artifact_files(source_root: Path, spec: ArtifactSpec) -> dict[str, bytes]:
    """Read only the exact path/glob allowlist for one role."""
    selected = set(spec.paths)
    for pattern in spec.globs:
        for path in source_root.glob(pattern):
            if path.is_file():
                selected.add(path.relative_to(source_root).as_posix())
    if not selected:
        raise ApplicationArtifactError(f"artifact allowlist is empty: {spec.kind}")
    files = {relative: _read_source_file(source_root, relative) for relative in sorted(selected)}
    if spec.kind in {"api", "public-runtime", "web"}:
        forbidden_roots = {"apps/candidate_builder", "packages/legacy_bridge", "packages/publisher"}
        for relative in files:
            if any(relative == root or relative.startswith(root + "/") for root in forbidden_roots):
                raise ApplicationArtifactError(
                    f"public artifact contains publisher/editorial code: {relative}"
                )
    return files


def _role_manifest(spec: ArtifactSpec, files: Mapping[str, bytes]) -> bytes:
    records = [
        {
            "bytes": len(content),
            "mode": "0644",
            "path": path,
            "sha256": sha256_bytes(content),
        }
        for path, content in sorted(files.items())
    ]
    return canonical_json_bytes(
        {
            "artifactFormat": "radar-v2-role-artifact/v1",
            "buildEpoch": 0,
            "files": records,
            "pythonRuntime": "3.12",
            "role": spec.kind,
            "runtimeDependencies": [],
        }
    )


def _add_tar_file(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(content)
    info.mode = 0o644
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(content))


def _render_archive(prefix: str, files: Mapping[str, bytes]) -> bytes:
    entries = {f"{prefix}/{relative}": content for relative, content in files.items()}
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(entries):
            _add_tar_file(archive, name, entries[name])
    gzip_buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", compresslevel=9, fileobj=gzip_buffer, mtime=0
    ) as compressor:
        compressor.write(tar_buffer.getvalue())
    return gzip_buffer.getvalue()


def _parse_archive(content: bytes, prefix: str) -> dict[str, bytes]:
    if len(content) > _MAX_FILE_BYTES:
        raise ApplicationArtifactError("compressed artifact exceeds 64 MiB")
    expected_prefix = prefix + "/"
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise ApplicationArtifactError("artifact contains too many members")
            if [member.name for member in members] != sorted(member.name for member in members):
                raise ApplicationArtifactError("artifact members are not sorted")
            for member in members:
                if (
                    not member.isfile()
                    or member.mode != 0o644
                    or member.mtime != 0
                    or member.uid != 0
                    or member.gid != 0
                    or member.uname
                    or member.gname
                    or not member.name.startswith(expected_prefix)
                ):
                    raise ApplicationArtifactError(f"non-normalized artifact member: {member.name}")
                relative = member.name[len(expected_prefix) :]
                try:
                    relative_parts(relative)
                except SafeFilesystemError as error:
                    raise ApplicationArtifactError(
                        f"artifact member path is unsafe: {member.name}"
                    ) from error
                if relative in files:
                    raise ApplicationArtifactError(f"duplicate artifact member: {relative}")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ApplicationArtifactError(f"artifact member cannot be read: {relative}")
                payload = extracted.read(_MAX_FILE_BYTES + 1)
                if len(payload) > _MAX_FILE_BYTES:
                    raise ApplicationArtifactError(f"artifact member exceeds 64 MiB: {relative}")
                files[relative] = payload
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ApplicationArtifactError(f"artifact archive is invalid: {error}") from error
    if not files:
        raise ApplicationArtifactError("artifact archive is empty")
    return files


def build_role_artifact(source_root: Path, spec: ArtifactSpec) -> BuiltRoleArtifact:
    """Build and immediately re-validate one normalized role artifact."""
    source_files = collect_artifact_files(source_root, spec)
    manifest = _role_manifest(spec, source_files)
    entries = {ROLE_MANIFEST_NAME: manifest, **source_files}
    content = _render_archive(spec.prefix, entries)
    parsed = parse_role_artifact(content, spec)
    if parsed != entries:
        raise ApplicationArtifactError(f"role artifact changed during render: {spec.kind}")
    return BuiltRoleArtifact(
        spec=spec,
        content=content,
        manifest=manifest,
        sha256=sha256_bytes(content),
        files=tuple(sorted(source_files)),
    )


def parse_role_artifact(content: bytes, spec: ArtifactSpec) -> dict[str, bytes]:
    """Validate a role archive against its embedded hashes and exact allowlist."""
    files = _parse_archive(content, spec.prefix)
    try:
        raw_manifest = files.pop(ROLE_MANIFEST_NAME)
    except KeyError as error:
        raise ApplicationArtifactError("role artifact lacks MANIFEST.json") from error
    try:
        parsed = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ApplicationArtifactError(f"role manifest JSON is invalid: {error}") from error
    if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != raw_manifest:
        raise ApplicationArtifactError("role manifest is not canonical JSON")
    if set(parsed) != {
        "artifactFormat",
        "buildEpoch",
        "files",
        "pythonRuntime",
        "role",
        "runtimeDependencies",
    }:
        raise ApplicationArtifactError("role manifest has unknown or missing fields")
    if (
        parsed["artifactFormat"] != "radar-v2-role-artifact/v1"
        or parsed["buildEpoch"] != 0
        or parsed["pythonRuntime"] != "3.12"
        or parsed["role"] != spec.kind
        or parsed["runtimeDependencies"] != []
    ):
        raise ApplicationArtifactError("role manifest identity differs from its spec")
    records = parsed["files"]
    if not isinstance(records, list) or len(records) != len(files):
        raise ApplicationArtifactError("role manifest file count differs")
    expected_paths = tuple(sorted(collect_artifact_paths(spec, files)))
    if tuple(sorted(files)) != expected_paths:
        raise ApplicationArtifactError("role artifact membership differs from its allowlist")
    for record, path in zip(records, expected_paths, strict=True):
        if not isinstance(record, dict) or set(record) != {"bytes", "mode", "path", "sha256"}:
            raise ApplicationArtifactError("role manifest record is invalid")
        payload = files[path]
        if (
            record["path"] != path
            or record["mode"] != "0644"
            or record["bytes"] != len(payload)
            or record["sha256"] != sha256_bytes(payload)
        ):
            raise ApplicationArtifactError(f"role manifest digest differs: {path}")
    normalized = {ROLE_MANIFEST_NAME: raw_manifest, **files}
    if _render_archive(spec.prefix, normalized) != content:
        raise ApplicationArtifactError("role artifact gzip/tar bytes are not canonical")
    return normalized


def collect_artifact_paths(spec: ArtifactSpec, files: Mapping[str, bytes]) -> set[str]:
    """Validate that expanded files match exact paths plus declared globs."""
    expected = set(spec.paths)
    for path in files:
        if path in expected:
            continue
        if not any(Path(path).match(pattern) for pattern in spec.globs):
            raise ApplicationArtifactError(f"undeclared file in {spec.kind} artifact: {path}")
        expected.add(path)
    missing = sorted(set(spec.paths) - set(files))
    if missing:
        raise ApplicationArtifactError(
            f"required files missing from {spec.kind}: {', '.join(missing)}"
        )
    return expected


def _source_files_sha256(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256(b"radar-v2-application-source/v1\0")
    for path, content in sorted(files.items()):
        encoded = path.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _source_tree_sha256(source_root: Path, artifacts: tuple[BuiltRoleArtifact, ...]) -> str:
    paths = sorted({path for artifact in artifacts for path in artifact.files})
    return _source_files_sha256({path: _read_source_file(source_root, path) for path in paths})


def _parse_provenance(content: bytes, manifest: ApplicationManifest) -> ApplicationProvenance:
    try:
        parsed = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ApplicationArtifactError(f"provenance JSON is invalid: {error}") from error
    if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != content:
        raise ApplicationArtifactError("provenance is not canonical JSON")
    if set(parsed) != {
        "applicationReleaseId",
        "createdAt",
        "format",
        "gitCommit",
        "gitTag",
        "sourceTreeSha256",
    }:
        raise ApplicationArtifactError("provenance has unknown or missing fields")
    source_digest = parsed["sourceTreeSha256"]
    git_tag = parsed["gitTag"]
    if (
        parsed["format"] != "radar-v2-application-provenance/v1"
        or parsed["applicationReleaseId"] != manifest.application_release_id
        or parsed["gitCommit"] != manifest.git_commit
        or parsed["createdAt"] != manifest.created_at
        or not isinstance(source_digest, str)
        or len(source_digest) != 64
        or any(character not in "0123456789abcdef" for character in source_digest)
        or (git_tag is not None and (not isinstance(git_tag, str) or not git_tag))
    ):
        raise ApplicationArtifactError("provenance differs from the compatibility manifest")
    return ApplicationProvenance(
        application_release_id=manifest.application_release_id,
        git_commit=manifest.git_commit,
        git_tag=git_tag,
        created_at=manifest.created_at,
        source_tree_sha256=source_digest,
        document=cast(JsonObject, parsed),
    )


def build_application_release(
    source_root: Path,
    *,
    application_release_id: str,
    git_commit: str,
    created_at: str,
    git_tag: str | None = None,
) -> BuiltApplicationRelease:
    """Build the three role archives and one provenance-bound outer release package."""
    artifacts = tuple(build_role_artifact(source_root, spec) for spec in APPLICATION_ARTIFACTS)
    runtime = inspect_sqlite_runtime()
    manifest_document: dict[str, object] = {
        "applicationReleaseId": application_release_id,
        "artifacts": [
            {
                "bytes": len(artifact.content),
                "kind": artifact.spec.kind,
                "name": artifact.spec.name,
                "sha256": artifact.sha256,
            }
            for artifact in artifacts
        ],
        "candidateContractVersions": ["1.0.0"],
        "contractVersion": "1.0.0",
        "createdAt": created_at,
        "deltaContractVersions": ["1.0.0"],
        "gazetteContractVersions": ["1.0.0"],
        "gitCommit": git_commit,
        "manifestKind": "application",
        "publicApiVersion": "1.0.0",
        "resultContractVersions": ["1.0.0"],
        "schemaVersion": 1,
        "sqliteRuntime": {
            "compileOptions": sorted(runtime.compile_options),
            "sourceId": runtime.source_id,
            "version": runtime.version,
        },
        "tableContractVersion": "1.0.0",
    }
    manifest_bytes = canonical_json_bytes(manifest_document)
    manifest = parse_application_manifest(manifest_bytes)
    source_digest = _source_tree_sha256(source_root, artifacts)
    provenance_document: dict[str, object] = {
        "applicationReleaseId": application_release_id,
        "createdAt": created_at,
        "format": "radar-v2-application-provenance/v1",
        "gitCommit": git_commit,
        "gitTag": git_tag,
        "sourceTreeSha256": source_digest,
    }
    provenance_bytes = canonical_json_bytes(provenance_document)
    provenance = _parse_provenance(provenance_bytes, manifest)
    payloads = {
        COMPATIBILITY_MANIFEST_NAME: manifest_bytes,
        PROVENANCE_NAME: provenance_bytes,
        **{artifact.spec.name: artifact.content for artifact in artifacts},
    }
    checksums = "".join(
        f"{sha256_bytes(payloads[name])}  {name}\n" for name in sorted(payloads)
    ).encode("ascii")
    outer = _render_archive(
        APPLICATION_PACKAGE_PREFIX,
        {CHECKSUMS_NAME: checksums, **payloads},
    )
    parsed = parse_application_release(outer)
    if (
        parsed.manifest != manifest
        or parsed.provenance != provenance
        or parsed.artifacts != {artifact.spec.name: artifact.content for artifact in artifacts}
    ):
        raise ApplicationArtifactError("application release changed during render")
    return BuiltApplicationRelease(
        content=outer,
        sha256=sha256_bytes(outer),
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        provenance=provenance,
        provenance_bytes=provenance_bytes,
        artifacts=artifacts,
    )


def parse_application_release(content: bytes) -> ParsedApplicationRelease:
    """Validate the complete outer package, all checksums and every inner role archive."""
    files = _parse_archive(content, APPLICATION_PACKAGE_PREFIX)
    required = {
        CHECKSUMS_NAME,
        COMPATIBILITY_MANIFEST_NAME,
        PROVENANCE_NAME,
        *(spec.name for spec in APPLICATION_ARTIFACTS),
    }
    if set(files) != required:
        raise ApplicationArtifactError("application package membership is incomplete or unknown")
    manifest_bytes = files[COMPATIBILITY_MANIFEST_NAME]
    manifest = parse_application_manifest(manifest_bytes)
    descriptors = {descriptor.name: descriptor for descriptor in manifest.artifacts}
    selected_source_files: dict[str, bytes] = {}
    for spec in APPLICATION_ARTIFACTS:
        descriptor = descriptors.get(spec.name)
        payload = files[spec.name]
        if (
            descriptor is None
            or descriptor.kind != spec.kind
            or descriptor.bytes != len(payload)
            or descriptor.sha256 != sha256_bytes(payload)
        ):
            raise ApplicationArtifactError(f"compatibility descriptor differs: {spec.name}")
        role_files = parse_role_artifact(payload, spec)
        for path, role_content in role_files.items():
            if path == ROLE_MANIFEST_NAME:
                continue
            existing = selected_source_files.get(path)
            if existing is not None and existing != role_content:
                raise ApplicationArtifactError(f"role artifacts disagree on source file: {path}")
            selected_source_files[path] = role_content
    provenance_bytes = files[PROVENANCE_NAME]
    provenance = _parse_provenance(provenance_bytes, manifest)
    if provenance.source_tree_sha256 != _source_files_sha256(selected_source_files):
        raise ApplicationArtifactError(
            "provenance sourceTreeSha256 differs from role artifact bytes"
        )
    checksum_inputs = {name: payload for name, payload in files.items() if name != CHECKSUMS_NAME}
    expected_checksums = "".join(
        f"{sha256_bytes(checksum_inputs[name])}  {name}\n" for name in sorted(checksum_inputs)
    ).encode("ascii")
    if files[CHECKSUMS_NAME] != expected_checksums:
        raise ApplicationArtifactError("application package checksums are not canonical or differ")
    if _render_archive(APPLICATION_PACKAGE_PREFIX, files) != content:
        raise ApplicationArtifactError("application package gzip/tar bytes are not canonical")
    return ParsedApplicationRelease(
        sha256=sha256_bytes(content),
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        provenance=provenance,
        provenance_bytes=provenance_bytes,
        artifacts={spec.name: files[spec.name] for spec in APPLICATION_ARTIFACTS},
    )


__all__ = [
    "API_ARTIFACT",
    "APPLICATION_ARTIFACTS",
    "APPLICATION_PACKAGE_NAME",
    "APPLICATION_PACKAGE_PREFIX",
    "CHECKSUMS_NAME",
    "COMPATIBILITY_MANIFEST_NAME",
    "MIGRATION_ARTIFACT",
    "PROVENANCE_NAME",
    "PUBLIC_PRODUCTION_ARTIFACT",
    "ROLE_MANIFEST_NAME",
    "WEB_ARTIFACT",
    "ApplicationArtifactError",
    "ApplicationProvenance",
    "ArtifactSpec",
    "BuiltApplicationRelease",
    "BuiltRoleArtifact",
    "ParsedApplicationRelease",
    "build_application_release",
    "build_role_artifact",
    "collect_artifact_files",
    "parse_application_release",
    "parse_role_artifact",
    "sha256_bytes",
]
