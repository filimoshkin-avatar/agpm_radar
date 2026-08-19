"""Immutable Stage 5 candidate packages and Project Manager build boundary."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from packages.domain.candidate_mutations import (
    CandidateMutationError,
    build_candidate_mutations,
)
from packages.domain.candidates import (
    CandidateValidationError,
    candidate_bytes,
    parse_candidate_bytes,
    validate_candidate,
    validate_safe_text,
)
from packages.domain.dual_run import (
    ATTESTATION_NAME,
    BranchWorkspace,
    verify_consumption_attestation,
)
from packages.domain.snapshot import (
    JsonObject,
    SnapshotError,
    SnapshotIdentity,
    canonical_json_line,
    verify_snapshot,
)
from packages.storage.replication_mutations import (
    MUTATION_FORMAT,
    ReplayReport,
    replay_to_staging,
    validate_mutation_document,
)
from packages.storage.safe_files import (
    ArtifactExistsError,
    SafeFilesystemError,
    ensure_private_directory,
    open_directory_nofollow,
    open_regular_file_nofollow,
    publish_tree_directory,
    read_regular_file,
    read_tree_files,
    relative_parts,
)

PACKAGE_FORMAT: Final = "radar-candidate-package/v1"
MANIFEST_NAME: Final = "manifest.json"
CHECKSUMS_NAME: Final = "checksums.sha256"
PREVIEW_NAME: Final = "preview.txt"
MUTATIONS_NAME: Final = "payload/replication-mutations.json"
METADATA_NAME: Final = "payload/package-metadata.json"
SNAPSHOT_ATTESTATION_NAME: Final = "payload/snapshot-attestation.json"
_LOCK_NAME: Final = ".candidate-builder.lock"
_SHA256: Final = frozenset("0123456789abcdef")


class CandidatePackageError(RuntimeError):
    """A candidate package cannot be created or verified safely."""


class CandidateDuplicateError(CandidatePackageError):
    """A candidate or idempotency key has already been registered."""


@dataclass(frozen=True, slots=True)
class CandidatePackage:
    """Verified immutable machine package and human preview."""

    path: Path
    candidate: JsonObject
    mutations: JsonObject
    package_sha256: str
    preview: str


@dataclass(frozen=True, slots=True)
class CandidateBuildResult:
    """Complete Stage 5 build evidence retained for the caller."""

    package: CandidatePackage
    replay: ReplayReport


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parse_canonical_value(content: bytes, label: str) -> object:
    try:
        value: object = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidatePackageError(f"invalid JSON in {label}: {error}") from error
    if canonical_json_line(value) != content:
        raise CandidatePackageError(f"{label} is not canonical JSON")
    return value


def _canonical_object(content: bytes, label: str) -> JsonObject:
    value = _parse_canonical_value(content, label)
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CandidatePackageError(f"{label} must be a JSON object")
    return cast(JsonObject, value)


def _render_checksums(files: Mapping[str, bytes]) -> bytes:
    for path in files:
        relative_parts(path)
    return "".join(f"{_sha256(files[path])}  {path}\n" for path in sorted(files)).encode("ascii")


def _preview(candidate: Mapping[str, object], mutations: Mapping[str, object]) -> str:
    completeness = cast(dict[str, object], mutations["completeness"])
    operation = cast(str, candidate["operation"])
    llm = cast(dict[str, object], candidate["llmOutcome"])
    lines = [
        "Radar V2 candidate preview",
        f"Candidate: {candidate['candidateId']}",
        f"Operation: {operation}",
        f"Reason: {candidate['reason']}",
        f"Base release: {cast(dict[str, object], candidate['expectedBase'])['releaseId']}",
        f"LLM outcome: {llm['status']}",
        f"Affected tables: {completeness['affectedTableCount']}",
        f"Typed mutations: {completeness['totalMutationCount']}",
    ]
    if operation in {"daily", "correction"}:
        issue = cast(dict[str, object], candidate["desiredIssue"])
        lines.extend(
            [
                f"Issue date: {issue['issueDate']}",
                f"Included materials: {len(cast(list[object], issue['materials']))}",
            ]
        )
    else:
        lines.extend(
            [
                f"Gazette period: {candidate['period']}",
                f"Assets: {len(cast(list[object], candidate['inputAssets']))}",
            ]
        )
    if llm["status"] == "fallback":
        effective = cast(dict[str, str], llm["effective"])
        lines.append(
            "WARNING: primary model was not used; accepted fallback "
            f"{effective['provider']}/{effective['model']}."
        )
    elif llm["status"] == "unavailable":
        fallback = cast(dict[str, str], llm["deterministicFallback"])
        lines.append(
            "WARNING: all LLM output was unavailable; deterministic fallback "
            f"{fallback['implementation']}@{fallback['version']} is explicit."
        )
    elif llm["status"] == "not_requested":
        lines.append("LLM was not requested for this operation.")
    return "\n".join(lines) + "\n"


def _content_payloads(candidate: JsonObject) -> dict[str, bytes]:
    operation = candidate["operation"]
    if operation in {"daily", "correction"}:
        issue = cast(dict[str, object], candidate["desiredIssue"])
        materials = cast(list[dict[str, object]], issue["materials"])
        return {
            "payload/analyses.json": canonical_json_line(
                {
                    "issue": issue["analysis"],
                    "materials": [
                        {
                            "llmAgpmAngle": material["llmAgpmAngle"],
                            "llmShortText": material["llmShortText"],
                            "llmStatus": material["llmStatus"],
                            "materialId": material["materialId"],
                        }
                        for material in materials
                    ],
                }
            ),
            "payload/issue.json": canonical_json_line(issue),
            "payload/materials.json": canonical_json_line(materials),
            "payload/queue-changes.json": canonical_json_line(
                candidate["queueChanges"] if operation == "daily" else []
            ),
            "payload/stats.json": canonical_json_line(issue["stats"]),
        }
    return {
        "payload/gazette.json": canonical_json_line(
            {
                key: candidate[key]
                for key in (
                    "expectedGazette",
                    "gazetteId",
                    "htmlEntrypoint",
                    "inputAssets",
                    "ownerRequestDigest",
                    "period",
                    "title",
                )
            }
        )
    }


def _validate_assets(candidate: JsonObject, assets: Mapping[str, bytes] | None) -> dict[str, bytes]:
    if candidate["operation"] != "gazette":
        if assets:
            raise CandidatePackageError("daily/correction package cannot contain assets")
        return {}
    supplied = dict(assets or {})
    descriptors = cast(list[dict[str, object]], candidate["inputAssets"])
    expected_paths = {cast(str, descriptor["relativePath"]) for descriptor in descriptors}
    if set(supplied) != expected_paths:
        raise CandidatePackageError("gazette asset membership differs from candidate descriptors")
    packaged: dict[str, bytes] = {}
    for descriptor in descriptors:
        path = cast(str, descriptor["relativePath"])
        relative_parts(path)
        content = supplied[path]
        if not isinstance(content, bytes):
            raise CandidatePackageError(f"gazette asset is not exact bytes: {path}")
        if len(content) != descriptor["bytes"] or _sha256(content) != descriptor["sha256"]:
            raise CandidatePackageError(f"gazette asset bytes/hash differ: {path}")
        media_type = cast(str, descriptor["mediaType"])
        try:
            inspected_text = content.decode(
                "utf-8",
                errors="strict"
                if media_type in {"text/html", "text/css", "image/svg+xml"}
                else "ignore",
            )
        except UnicodeDecodeError as error:
            raise CandidatePackageError(f"text gazette asset is not UTF-8: {path}") from error
        try:
            validate_safe_text(inspected_text, f"gazette asset {path}")
        except CandidateValidationError as error:
            raise CandidatePackageError(str(error)) from error
        packaged[f"assets/{path}"] = content
    return packaged


def _snapshot_evidence(
    candidate: JsonObject,
    workspace: BranchWorkspace | None,
) -> tuple[bytes | None, str | None, SnapshotIdentity | None, str | None]:
    if candidate["operation"] != "daily":
        if workspace is not None:
            raise CandidatePackageError("non-daily candidate cannot carry a snapshot workspace")
        return None, None, None, None
    if workspace is None or workspace.branch != "v2":
        raise CandidatePackageError("daily candidate requires the verified V2 branch workspace")
    attestation = verify_consumption_attestation(workspace)
    snapshot = verify_snapshot(workspace.area("input") / workspace.snapshot_id)
    if attestation.identity != snapshot.identity:
        raise CandidatePackageError("V2 attestation identity differs from snapshot bytes")
    expected = cast(dict[str, object], candidate["snapshot"])
    if expected != {
        "snapshotId": snapshot.identity.snapshot_id,
        "manifestSha256": snapshot.identity.manifest_sha256,
        "payloadSha256": snapshot.identity.payload_sha256,
        "itemCount": snapshot.identity.item_count,
    }:
        raise CandidatePackageError("daily candidate differs from its V2 snapshot evidence")
    content = read_regular_file(
        workspace.area("attestations") / ATTESTATION_NAME,
        expected_mode=0o400,
    )
    if _sha256(content) != attestation.attestation_sha256:
        raise CandidatePackageError("V2 attestation bytes changed after verification")
    return (
        content,
        snapshot.identity.checksums_sha256,
        snapshot.identity,
        snapshot.collected_at,
    )


def _package_files(
    candidate: JsonObject,
    mutations: JsonObject,
    replay: ReplayReport,
    *,
    snapshot_attestation: bytes | None,
    snapshot_checksums_sha256: str | None,
    packaged_assets: Mapping[str, bytes],
) -> dict[str, bytes]:
    manifest = candidate_bytes(candidate)
    completeness = cast(JsonObject, mutations["completeness"])
    metadata = {
        "candidateSha256": _sha256(manifest),
        "llmStatus": cast(dict[str, object], candidate["llmOutcome"])["status"],
        "mutationCompleteness": completeness,
        "mutationFormat": MUTATION_FORMAT,
        "operation": candidate["operation"],
        "packageFormat": PACKAGE_FORMAT,
        "snapshotChecksumsSha256": snapshot_checksums_sha256,
        "stagingAfterStateHash": replay.after_state_hash,
        "stagingApplied": replay.applied,
        "stagingIdempotentSkips": replay.idempotent_skips,
    }
    files = {
        MANIFEST_NAME: manifest,
        METADATA_NAME: canonical_json_line(metadata),
        MUTATIONS_NAME: canonical_json_line(mutations),
        PREVIEW_NAME: _preview(candidate, mutations).encode("utf-8"),
        **_content_payloads(candidate),
        **packaged_assets,
    }
    if snapshot_attestation is not None:
        files[SNAPSHOT_ATTESTATION_NAME] = snapshot_attestation
    files[CHECKSUMS_NAME] = _render_checksums(files)
    return files


def _parse_checksums(content: bytes) -> dict[str, str]:
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as error:
        raise CandidatePackageError("checksums.sha256 is not ASCII") from error
    if not text or not text.endswith("\n"):
        raise CandidatePackageError("checksums.sha256 is empty or truncated")
    records: dict[str, str] = {}
    prior = ""
    for line in text.splitlines():
        digest, separator, path = line.partition("  ")
        if (
            separator != "  "
            or len(digest) != 64
            or any(character not in _SHA256 for character in digest)
        ):
            raise CandidatePackageError("checksums.sha256 contains an invalid digest line")
        relative_parts(path)
        if path <= prior or path in records or path == CHECKSUMS_NAME:
            raise CandidatePackageError("checksums paths must be unique and lexically ordered")
        records[path] = digest
        prior = path
    return records


def _expected_members(candidate: JsonObject) -> set[str]:
    common = {MANIFEST_NAME, CHECKSUMS_NAME, PREVIEW_NAME, MUTATIONS_NAME, METADATA_NAME}
    if candidate["operation"] in {"daily", "correction"}:
        common |= {
            "payload/analyses.json",
            "payload/issue.json",
            "payload/materials.json",
            "payload/queue-changes.json",
            "payload/stats.json",
        }
        if candidate["operation"] == "daily":
            common.add(SNAPSHOT_ATTESTATION_NAME)
    else:
        common.add("payload/gazette.json")
        common |= {
            f"assets/{cast(dict[str, object], asset)['relativePath']}"
            for asset in cast(list[object], candidate["inputAssets"])
        }
    return common


def _verify_payload_views(files: Mapping[str, bytes], candidate: JsonObject) -> None:
    expected = _content_payloads(candidate)
    for path, content in expected.items():
        if files[path] != content:
            raise CandidatePackageError(f"candidate payload view differs: {path}")


def _verify_snapshot_attestation(
    content: bytes,
    candidate: JsonObject,
    expected_checksums_sha256: object,
) -> None:
    attestation = _canonical_object(content, SNAPSHOT_ATTESTATION_NAME)
    if set(attestation) != {
        "attestationFormat",
        "branch",
        "consumedAt",
        "snapshot",
        "snapshotRelativePath",
    }:
        raise CandidatePackageError("snapshot attestation shape differs")
    snapshot = cast(dict[str, object], attestation["snapshot"])
    candidate_snapshot = cast(dict[str, object], candidate["snapshot"])
    if attestation["branch"] != "v2" or snapshot != {
        "checksumsSha256": expected_checksums_sha256,
        "itemCount": candidate_snapshot["itemCount"],
        "manifestSha256": candidate_snapshot["manifestSha256"],
        "payloadSha256": candidate_snapshot["payloadSha256"],
        "snapshotId": candidate_snapshot["snapshotId"],
    }:
        raise CandidatePackageError("snapshot attestation is not bound to the daily candidate")


def verify_candidate_package(path: Path) -> CandidatePackage:
    """Read exact immutable bytes and validate membership, checksums and all bindings."""
    try:
        files = read_tree_files(path)
        candidate = parse_candidate_bytes(files[MANIFEST_NAME])
        if path.name != candidate["candidateId"]:
            raise CandidatePackageError("package directory name differs from candidateId")
        expected_members = _expected_members(candidate)
        if set(files) != expected_members:
            raise CandidatePackageError("candidate package membership differs from its operation")
        checksums = _parse_checksums(files[CHECKSUMS_NAME])
        if set(checksums) != expected_members - {CHECKSUMS_NAME}:
            raise CandidatePackageError("checksums membership differs from package membership")
        for member, digest in checksums.items():
            if _sha256(files[member]) != digest:
                raise CandidatePackageError(f"candidate package checksum differs: {member}")
        mutations = _canonical_object(files[MUTATIONS_NAME], MUTATIONS_NAME)
        validate_mutation_document(mutations, candidate)
        metadata = _canonical_object(files[METADATA_NAME], METADATA_NAME)
        expected_metadata_keys = {
            "candidateSha256",
            "llmStatus",
            "mutationCompleteness",
            "mutationFormat",
            "operation",
            "packageFormat",
            "snapshotChecksumsSha256",
            "stagingAfterStateHash",
            "stagingApplied",
            "stagingIdempotentSkips",
        }
        if set(metadata) != expected_metadata_keys:
            raise CandidatePackageError("package metadata shape differs")
        if (
            metadata["packageFormat"] != PACKAGE_FORMAT
            or metadata["operation"] != candidate["operation"]
            or metadata["candidateSha256"] != _sha256(files[MANIFEST_NAME])
            or metadata["mutationFormat"] != MUTATION_FORMAT
            or metadata["mutationCompleteness"] != mutations["completeness"]
            or metadata["llmStatus"] != cast(dict[str, object], candidate["llmOutcome"])["status"]
        ):
            raise CandidatePackageError("package metadata is not bound to candidate/mutations")
        state_hash = metadata["stagingAfterStateHash"]
        if (
            not isinstance(state_hash, str)
            or len(state_hash) != 64
            or any(character not in _SHA256 for character in state_hash)
        ):
            raise CandidatePackageError("stagingAfterStateHash is invalid")
        for key in ("stagingApplied", "stagingIdempotentSkips"):
            if (
                not isinstance(metadata[key], int)
                or isinstance(metadata[key], bool)
                or cast(int, metadata[key]) < 0
            ):
                raise CandidatePackageError(f"{key} is invalid")
        _verify_payload_views(files, candidate)
        preview = files[PREVIEW_NAME].decode("utf-8")
        if preview != _preview(candidate, mutations):
            raise CandidatePackageError("human preview differs from machine candidate")
        if candidate["operation"] == "daily":
            _verify_snapshot_attestation(
                files[SNAPSHOT_ATTESTATION_NAME],
                candidate,
                metadata["snapshotChecksumsSha256"],
            )
        elif metadata["snapshotChecksumsSha256"] is not None:
            raise CandidatePackageError("non-daily package claims snapshot checksums")
        if candidate["operation"] == "gazette":
            descriptors = cast(list[dict[str, object]], candidate["inputAssets"])
            _validate_assets(
                candidate,
                {
                    cast(str, descriptor["relativePath"]): files[
                        f"assets/{descriptor['relativePath']}"
                    ]
                    for descriptor in descriptors
                },
            )
        return CandidatePackage(
            path=path,
            candidate=candidate,
            mutations=mutations,
            package_sha256=_sha256(files[CHECKSUMS_NAME]),
            preview=preview,
        )
    except (
        KeyError,
        UnicodeDecodeError,
        OSError,
        SafeFilesystemError,
        CandidateValidationError,
    ) as error:
        if isinstance(error, CandidatePackageError):
            raise
        raise CandidatePackageError(
            f"candidate package verification failed closed: {error}"
        ) from error


@contextmanager
def _candidate_store_lock(store: Path) -> Iterator[None]:
    ensure_private_directory(store)
    root = open_directory_nofollow(store)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            _LOCK_NAME,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise CandidatePackageError("candidate store lock is not a private single-link file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        os.close(root)


def _check_store_uniqueness(store: Path, candidate: JsonObject) -> None:
    for name in sorted(os.listdir(store)):
        if name == _LOCK_NAME:
            continue
        existing_path = store / name
        existing = verify_candidate_package(existing_path)
        if existing.candidate["candidateId"] == candidate["candidateId"]:
            raise CandidateDuplicateError("candidate id is already registered")
        if existing.candidate["idempotencyKey"] == candidate["idempotencyKey"]:
            raise CandidateDuplicateError("idempotency key is already registered")


@contextmanager
def _read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    descriptor = open_regular_file_nofollow(path)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
            uri=True,
        )
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        if connection is not None:
            connection.close()
        os.close(descriptor)


def build_candidate_package(
    *,
    source_database: Path,
    staging_database: Path,
    package_store: Path,
    candidate: Mapping[str, object],
    v2_workspace: BranchWorkspace | None = None,
    assets: Mapping[str, bytes] | None = None,
) -> CandidateBuildResult:
    """Generate, replay and atomically register one complete Stage 5 package."""
    try:
        validated = validate_candidate(candidate)
        (
            attestation_bytes,
            snapshot_checksums_sha256,
            snapshot_identity,
            snapshot_collected_at,
        ) = _snapshot_evidence(validated, v2_workspace)
        packaged_assets = _validate_assets(validated, assets)
        with _read_only_connection(source_database) as connection:
            connection.execute("BEGIN")
            plan = build_candidate_mutations(
                connection,
                validated,
                snapshot_identity=snapshot_identity,
                snapshot_collected_at=snapshot_collected_at,
            )
            if plan.source_state_hash != cast(
                str, cast(dict[str, object], validated["expectedBase"])["logicalStateHash"]
            ):
                raise CandidatePackageError("mutation plan lost its expected base binding")
            connection.rollback()
        with _candidate_store_lock(package_store):
            _check_store_uniqueness(package_store, validated)
        replay = replay_to_staging(
            source_database,
            staging_database,
            plan.document,
            validated,
            expected_source_state_hash=plan.source_state_hash,
        )
        files = _package_files(
            validated,
            plan.document,
            replay,
            snapshot_attestation=attestation_bytes,
            snapshot_checksums_sha256=snapshot_checksums_sha256,
            packaged_assets=packaged_assets,
        )
        with _candidate_store_lock(package_store):
            _check_store_uniqueness(package_store, validated)
            package_path = publish_tree_directory(
                package_store,
                cast(str, validated["candidateId"]),
                files,
            )
        package = verify_candidate_package(package_path)
        return CandidateBuildResult(package=package, replay=replay)
    except (
        ArtifactExistsError,
        CandidateMutationError,
        CandidateValidationError,
        SafeFilesystemError,
        SnapshotError,
        sqlite3.Error,
    ) as error:
        if isinstance(error, CandidatePackageError):
            raise
        raise CandidatePackageError(f"candidate build failed closed: {error}") from error


__all__ = [
    "CHECKSUMS_NAME",
    "MANIFEST_NAME",
    "METADATA_NAME",
    "MUTATIONS_NAME",
    "PACKAGE_FORMAT",
    "PREVIEW_NAME",
    "SNAPSHOT_ATTESTATION_NAME",
    "CandidateBuildResult",
    "CandidateDuplicateError",
    "CandidatePackage",
    "CandidatePackageError",
    "build_candidate_package",
    "verify_candidate_package",
]
