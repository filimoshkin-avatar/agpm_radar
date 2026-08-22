#!/usr/bin/env python3
"""Bundle the file wiki for a snapshot (P27, slice 2.5a).

Runs on the control host, where the wiki is. Produces a deterministic tar.gz that
`radar_kx import-wiki-snapshot` reads on the host that holds KX.

Deterministic on purpose: members sorted by path, and every timestamp, owner and
mode normalised. An unchanged wiki must produce an identical bundle, because the
snapshot's identity is derived from its contents and a bundle that differs on
mtime alone would make every release point at a "new" wiki.

    python3 scripts/wiki_snapshot_bundle.py \\
        --root /root/.openclaw-projectmanager/workspace/knowledge/agpm \\
        --out /tmp/wiki-agpm.tar.gz
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import sys
import tarfile
from pathlib import Path

#: Never bundled: editor litter and caches are not the wiki.
SKIP_NAMES = frozenset({".DS_Store", "Thumbs.db", "__pycache__", ".git", ".obsidian"})


def _files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_NAMES for part in path.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        found.append(path)
    return found


def build(root: Path, out: Path) -> dict[str, object]:
    paths = _files(root)
    if not paths:
        raise SystemExit(f"no files under {root}")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in paths:
            content = path.read_bytes()
            info = tarfile.TarInfo(name=str(path.relative_to(root)))
            info.size = len(content)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", compresslevel=9, mtime=0) as handle:
        handle.write(buffer.getvalue())
    out.write_bytes(packed.getvalue())
    return {
        "root": str(root),
        "bundle": str(out),
        "files": len(paths),
        "rawBytes": sum(path.stat().st_size for path in paths),
        "bundleBytes": out.stat().st_size,
        "bundleSha256": hashlib.sha256(out.read_bytes()).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="wiki_snapshot_bundle")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/root/.openclaw-projectmanager/workspace/knowledge/agpm"),
    )
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()
    for key, value in build(arguments.root, arguments.out).items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
