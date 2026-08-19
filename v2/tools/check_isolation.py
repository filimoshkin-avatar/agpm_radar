"""Fail closed when the Radar V2 workspace contains secrets or isolation violations."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final, cast

V2_ROOT: Final = Path(__file__).resolve().parents[1]
IGNORED_PARTS: Final = frozenset(
    {".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "dist"}
)
REQUIRED_DIRECTORIES: Final = (
    "apps/api",
    "apps/web",
    "packages/contracts",
    "packages/domain",
    "packages/storage",
    "packages/publisher",
    "packages/delta",
    "packages/renderers",
    "packages/validation",
    "packages/legacy_bridge",
    "fixtures/synthetic",
)
FORBIDDEN_PATH_PARTS: Final = frozenset({"corpus", "raw", "credentials", "secrets"})
FORBIDDEN_FILENAMES: Final = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
)
FORBIDDEN_DB_SUFFIXES: Final = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
)
SECRET_PATTERNS: Final = (
    re.compile(r"-----BEGIN (?:EC |OPENSSH |RSA )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}\b"),
)
RUNTIME_FORBIDDEN_FRAGMENTS: Final = (
    "/etc/",
    "/mnt/",
    "/root/",
    ".openclaw",
    "147.45.99.225",
    "backend/",
    "data/corpus",
    "data/db",
    "pipeline/",
    "work/",
)
ALLOWED_RUNTIME_IMPORT_ROOTS: Final = frozenset(
    {
        "__future__",
        "apps",
        "dataclasses",
        "fcntl",
        "hashlib",
        "json",
        "os",
        "packages",
        "pathlib",
        "re",
        "sqlite3",
        "stat",
        "typing",
        "unicodedata",
        "urllib",
    }
)


def workspace_files() -> Iterator[Path]:
    """Yield non-generated files without following generated environment content."""
    for path in sorted(V2_ROOT.rglob("*")):
        relative = path.relative_to(V2_ROOT)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_file() or path.is_symlink():
            yield path


def runtime_files() -> Iterator[Path]:
    """Yield application/package source that must stay independent of Legacy and OpenClaw."""
    for relative_root in (Path("apps"), Path("packages")):
        root = V2_ROOT / relative_root
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                yield path


def imported_roots(path: Path) -> Iterator[str]:
    """Extract absolute import roots from a Python source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.partition(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.partition(".")[0]


def scan_workspace() -> tuple[list[str], int, int]:
    """Return failures plus scanned-file and synthetic-fixture counts."""
    failures: list[str] = []
    scanned = 0
    fixtures = 0

    for relative_directory in REQUIRED_DIRECTORIES:
        if not (V2_ROOT / relative_directory).is_dir():
            failures.append(f"missing required directory: {relative_directory}")

    for path in workspace_files():
        scanned += 1
        relative = path.relative_to(V2_ROOT)
        lower_parts = {part.lower() for part in relative.parts}
        lower_name = path.name.lower()
        if path.is_symlink():
            failures.append(f"symlink is not allowed in V2 source: {relative}")
            continue
        if lower_parts & FORBIDDEN_PATH_PARTS:
            failures.append(f"raw/credential path is not allowed: {relative}")
        if lower_name in FORBIDDEN_FILENAMES or lower_name.startswith(".env."):
            failures.append(f"credential filename is not allowed: {relative}")
        if lower_name.endswith(FORBIDDEN_DB_SUFFIXES):
            failures.append(f"database file is not allowed: {relative}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"unexpected binary source file: {relative}")
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                failures.append(f"secret-shaped content in {relative}: {pattern.pattern}")

    for path in runtime_files():
        relative = path.relative_to(V2_ROOT)
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for fragment in RUNTIME_FORBIDDEN_FRAGMENTS:
            if fragment in lowered:
                failures.append(f"runtime isolation violation in {relative}: {fragment}")
        if path.suffix == ".py":
            for imported_root in imported_roots(path):
                if imported_root not in ALLOWED_RUNTIME_IMPORT_ROOTS:
                    failures.append(
                        f"third-party/Legacy runtime import in {relative}: {imported_root}"
                    )
        if path.suffix in {".js", ".mjs"}:
            external_import = re.search(r"(?:from\s+|import\s*\()[\"']([^./][^\"']*)[\"']", text)
            if external_import:
                failures.append(
                    f"external JavaScript dependency in {relative}: {external_import.group(1)}"
                )
        if path.suffix == ".html" and re.search(r"https?://", text, flags=re.IGNORECASE):
            failures.append(f"remote web dependency in {relative}")

    fixture_root = V2_ROOT / "fixtures"
    for path in sorted(fixture_root.rglob("*")):
        if not path.is_file():
            continue
        fixtures += 1
        relative = path.relative_to(V2_ROOT)
        if path.suffix != ".json":
            failures.append(f"fixture must be inspectable JSON: {relative}")
            continue
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            failures.append(f"fixture root must be an object: {relative}")
            continue
        fixture = cast(dict[str, object], parsed)
        if fixture.get("fixtureKind") != "synthetic":
            failures.append(f"fixture is not explicitly synthetic: {relative}")
        if fixture.get("containsProductionData") is not False:
            failures.append(f"fixture does not deny production data: {relative}")
    if fixtures == 0:
        failures.append("at least one synthetic fixture is required")

    return failures, scanned, fixtures


def main() -> int:
    """Run the secret and isolation scan."""
    failures, scanned, fixtures = scan_workspace()
    if failures:
        print("Radar V2 secret/isolation scan: FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Radar V2 secret/isolation scan: PASS")
    print(f"Files scanned: {scanned}")
    print(f"Synthetic fixtures: {fixtures}")
    print("Runtime imports: Python stdlib/local only; browser module dependency-free")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
