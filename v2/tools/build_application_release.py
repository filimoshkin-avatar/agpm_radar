"""Build a clean-Git, provenance-bound Radar V2 application release."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

V2_ROOT: Final = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT: Final = V2_ROOT.parent
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from packages.deployment.artifacts import (  # noqa: E402
    APPLICATION_ARTIFACTS,
    APPLICATION_PACKAGE_NAME,
    COMPATIBILITY_MANIFEST_NAME,
    PROVENANCE_NAME,
    build_application_release,
    collect_artifact_files,
)
from packages.storage.safe_files import atomic_write_new, ensure_private_directory  # noqa: E402

_TAG: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class GitProvenanceError(RuntimeError):
    """The requested Git commit/tag does not describe a clean tracked source tree."""


def _git(repository_root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(  # noqa: S603 -- argv is fixed or validated; no shell is used
            ["/usr/bin/git", *arguments],
            cwd=repository_root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise GitProvenanceError(
            f"Git provenance command failed: git {' '.join(arguments)}"
        ) from error
    return completed.stdout


def verify_clean_git_provenance(
    repository_root: Path,
    source_root: Path,
    *,
    git_commit: str,
    git_tag: str | None,
) -> None:
    """Require clean HEAD, optional exact tag and every artifact input tracked at HEAD."""
    repository = repository_root.resolve(strict=True)
    source = source_root.resolve(strict=True)
    try:
        source_prefix = source.relative_to(repository).as_posix()
    except ValueError as error:
        raise GitProvenanceError("application source root is outside its Git repository") from error
    if _git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise GitProvenanceError("application release requires a completely clean Git worktree")
    head = _git(repository, "rev-parse", "HEAD").decode("ascii").strip()
    if head != git_commit:
        raise GitProvenanceError("approved gitCommit is not the clean repository HEAD")
    if git_tag is not None:
        if not _TAG.fullmatch(git_tag) or ".." in git_tag or "//" in git_tag:
            raise GitProvenanceError("gitTag is not a safe bounded ref name")
        tagged = _git(repository, "rev-parse", f"refs/tags/{git_tag}^{{commit}}").decode().strip()
        if tagged != git_commit:
            raise GitProvenanceError("gitTag does not resolve to approved gitCommit")
    tracked = set(
        filter(
            None,
            _git(repository, "ls-files", "-z").decode("utf-8").split("\0"),
        )
    )
    selected = {
        f"{source_prefix}/{relative}"
        for spec in APPLICATION_ARTIFACTS
        for relative in collect_artifact_files(source, spec)
    }
    missing = sorted(selected - tracked)
    if missing:
        raise GitProvenanceError("application artifact inputs are untracked: " + ", ".join(missing))


def main(argv: list[str] | None = None) -> int:
    """Build, validate and write one immutable release package from clean HEAD."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--application-release-id", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--git-tag")
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--output-dir", type=Path, default=V2_ROOT / "dist/application-release")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    verify_clean_git_provenance(
        REPOSITORY_ROOT,
        V2_ROOT,
        git_commit=arguments.git_commit,
        git_tag=arguments.git_tag,
    )
    built = build_application_release(
        V2_ROOT,
        application_release_id=arguments.application_release_id,
        git_commit=arguments.git_commit,
        git_tag=arguments.git_tag,
        created_at=arguments.created_at,
    )
    if arguments.check:
        repeated = build_application_release(
            V2_ROOT,
            application_release_id=arguments.application_release_id,
            git_commit=arguments.git_commit,
            git_tag=arguments.git_tag,
            created_at=arguments.created_at,
        )
        if built.content != repeated.content:
            raise RuntimeError("application release is not deterministic across consecutive builds")

    output_dir: Path = arguments.output_dir
    output_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    ensure_private_directory(output_dir)
    atomic_write_new(output_dir / APPLICATION_PACKAGE_NAME, built.content, mode=0o600)
    atomic_write_new(
        output_dir / f"{APPLICATION_PACKAGE_NAME}.sha256",
        f"{built.sha256}  {APPLICATION_PACKAGE_NAME}\n".encode("ascii"),
        mode=0o600,
    )
    atomic_write_new(
        output_dir / COMPATIBILITY_MANIFEST_NAME,
        built.manifest_bytes,
        mode=0o600,
    )
    atomic_write_new(output_dir / PROVENANCE_NAME, built.provenance_bytes, mode=0o600)
    print("Radar V2 application release: PASS")
    print(f"Application release: {built.manifest.application_release_id}")
    print(f"Git commit: {built.manifest.git_commit}")
    print(f"Role artifacts: {len(built.artifacts)}")
    print(f"Package SHA-256: {built.sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
