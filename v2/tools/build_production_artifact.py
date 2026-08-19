"""Build and validate a deterministic allowlist-only Radar V2 production artifact."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Final

V2_ROOT: Final = Path(__file__).resolve().parents[1]
ARTIFACT_PREFIX: Final = "radar-v2-production"
ARTIFACT_NAME: Final = f"{ARTIFACT_PREFIX}.tar.gz"
MANIFEST_NAME: Final = "MANIFEST.json"
RUNTIME_GLOBS: Final = (
    "apps/__init__.py",
    "apps/api/**/*.py",
    "apps/web/**/*.css",
    "apps/web/**/*.html",
    "apps/web/**/*.js",
    "apps/web/**/*.mjs",
    "packages/__init__.py",
    "packages/**/*.py",
    "packages/**/*.sql",
)
FORBIDDEN_ARTIFACT_ROOTS: Final = frozenset(
    {".github", ".venv", "docs", "fixtures", "tests", "tools"}
)
FORBIDDEN_ARTIFACT_SUFFIXES: Final = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
)
FORBIDDEN_ARTIFACT_CONTENT: Final = (
    b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----",
    b"-----BEGIN " + b"PRIVATE KEY-----",
    b".openclaw",
)


def sha256_bytes(content: bytes) -> str:
    """Return the canonical SHA-256 hex digest."""
    return hashlib.sha256(content).hexdigest()


def collect_runtime_files() -> tuple[Path, ...]:
    """Collect only explicitly allowlisted runtime source files."""
    selected: set[Path] = set()
    for pattern in RUNTIME_GLOBS:
        selected.update(path for path in V2_ROOT.glob(pattern) if path.is_file())
    files = tuple(sorted(selected, key=lambda item: item.relative_to(V2_ROOT).as_posix()))
    if not files:
        raise RuntimeError("production artifact allowlist selected no files")
    for path in files:
        relative = path.relative_to(V2_ROOT)
        if path.is_symlink():
            raise RuntimeError(f"artifact source must not be a symlink: {relative}")
        if relative.parts[0] in FORBIDDEN_ARTIFACT_ROOTS:
            raise RuntimeError(f"forbidden artifact root selected: {relative}")
        if path.name.lower().endswith(FORBIDDEN_ARTIFACT_SUFFIXES):
            raise RuntimeError(f"database file selected for artifact: {relative}")
        content = path.read_bytes()
        for forbidden in FORBIDDEN_ARTIFACT_CONTENT:
            if forbidden.lower() in content.lower():
                raise RuntimeError(f"credential/OpenClaw content selected: {relative}")
    return files


def build_manifest(files: tuple[Path, ...]) -> bytes:
    """Create the canonical runtime-file manifest."""
    records: list[dict[str, object]] = []
    for path in files:
        content = path.read_bytes()
        records.append(
            {
                "bytes": len(content),
                "mode": "0644",
                "path": path.relative_to(V2_ROOT).as_posix(),
                "sha256": sha256_bytes(content),
            }
        )
    manifest: dict[str, object] = {
        "artifactFormat": "radar-v2-production/v1",
        "buildEpoch": 0,
        "contractFamily": "radar-v2/1",
        "excludedClasses": [
            "database-and-raw-corpus",
            "development-tools-and-tests",
            "fixtures",
            "openclaw-and-credentials",
        ],
        "files": records,
        "pythonRuntime": "3.12",
        "runtimeDependencies": [],
    }
    return (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def add_tar_file(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    """Add one regular file with normalized portable metadata."""
    info = tarfile.TarInfo(name=name)
    info.size = len(content)
    info.mode = 0o644
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(content))


def render_artifact(files: tuple[Path, ...]) -> tuple[bytes, bytes]:
    """Render normalized tar and gzip streams entirely in memory."""
    manifest = build_manifest(files)
    entries: dict[str, bytes] = {
        f"{ARTIFACT_PREFIX}/{MANIFEST_NAME}": manifest,
    }
    for path in files:
        relative = path.relative_to(V2_ROOT).as_posix()
        entries[f"{ARTIFACT_PREFIX}/{relative}"] = path.read_bytes()

    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(entries):
            add_tar_file(archive, name, entries[name])

    gzip_buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", compresslevel=9, fileobj=gzip_buffer, mtime=0
    ) as compressor:
        compressor.write(tar_buffer.getvalue())
    return gzip_buffer.getvalue(), manifest


def validate_artifact(artifact: bytes, manifest: bytes, files: tuple[Path, ...]) -> None:
    """Verify membership, normalized file types and every manifest hash."""
    expected_content = {
        f"{ARTIFACT_PREFIX}/{MANIFEST_NAME}": manifest,
        **{
            f"{ARTIFACT_PREFIX}/{path.relative_to(V2_ROOT).as_posix()}": path.read_bytes()
            for path in files
        },
    }
    with tarfile.open(fileobj=io.BytesIO(artifact), mode="r:gz") as archive:
        members = archive.getmembers()
        member_names = [member.name for member in members]
        if member_names != sorted(expected_content):
            raise RuntimeError("artifact membership/order differs from the allowlist")
        for member in members:
            if not member.isfile() or member.mode != 0o644 or member.mtime != 0:
                raise RuntimeError(f"non-normalized artifact member: {member.name}")
            extracted = archive.extractfile(member)
            if extracted is None or extracted.read() != expected_content[member.name]:
                raise RuntimeError(f"artifact content mismatch: {member.name}")

    parsed = json.loads(manifest)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("files"), list):
        raise RuntimeError("artifact manifest has an invalid shape")
    records = parsed["files"]
    if len(records) != len(files):
        raise RuntimeError("artifact manifest file count mismatch")
    for record, path in zip(records, files, strict=True):
        if not isinstance(record, dict):
            raise RuntimeError("artifact manifest record is not an object")
        content = path.read_bytes()
        if record.get("path") != path.relative_to(V2_ROOT).as_posix():
            raise RuntimeError("artifact manifest path/order mismatch")
        if record.get("bytes") != len(content) or record.get("sha256") != sha256_bytes(content):
            raise RuntimeError(f"artifact manifest digest mismatch: {path}")


def main() -> int:
    """Build the production artifact and optionally prove two-build determinism."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="render twice and compare exact bytes")
    parser.add_argument("--output-dir", type=Path, default=V2_ROOT / "dist")
    args = parser.parse_args()

    files = collect_runtime_files()
    artifact, manifest = render_artifact(files)
    if args.check:
        repeated_artifact, repeated_manifest = render_artifact(files)
        if artifact != repeated_artifact or manifest != repeated_manifest:
            raise RuntimeError("production artifact is not deterministic across consecutive builds")
    validate_artifact(artifact, manifest, files)

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = output_dir / ARTIFACT_NAME
    manifest_path = output_dir / f"{ARTIFACT_PREFIX}.manifest.json"
    digest_path = output_dir / f"{ARTIFACT_NAME}.sha256"
    artifact_path.write_bytes(artifact)
    manifest_path.write_bytes(manifest)
    digest = sha256_bytes(artifact)
    digest_path.write_text(f"{digest}  {ARTIFACT_NAME}\n", encoding="utf-8")

    print("Radar V2 production artifact: PASS")
    print(f"Runtime files: {len(files)}")
    print(f"Artifact SHA-256: {digest}")
    try:
        displayed_manifest_path = manifest_path.relative_to(V2_ROOT)
    except ValueError:
        displayed_manifest_path = manifest_path
    print(f"Manifest: {displayed_manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
