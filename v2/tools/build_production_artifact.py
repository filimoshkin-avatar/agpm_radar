"""Build the deterministic publisher-free Radar V2 public runtime artifact."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

V2_ROOT: Final = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from packages.deployment.artifacts import (  # noqa: E402
    PUBLIC_PRODUCTION_ARTIFACT,
    build_role_artifact,
    sha256_bytes,
)

ARTIFACT_PREFIX: Final = PUBLIC_PRODUCTION_ARTIFACT.prefix
ARTIFACT_NAME: Final = PUBLIC_PRODUCTION_ARTIFACT.name


def main() -> int:
    """Build the public artifact and optionally prove consecutive byte determinism."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="render twice and compare exact bytes")
    parser.add_argument("--output-dir", type=Path, default=V2_ROOT / "dist")
    arguments = parser.parse_args()

    built = build_role_artifact(V2_ROOT, PUBLIC_PRODUCTION_ARTIFACT)
    if arguments.check:
        repeated = build_role_artifact(V2_ROOT, PUBLIC_PRODUCTION_ARTIFACT)
        if built.content != repeated.content or built.manifest != repeated.manifest:
            raise RuntimeError("public production artifact is not deterministic")

    output_dir: Path = arguments.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = output_dir / ARTIFACT_NAME
    manifest_path = output_dir / f"{ARTIFACT_PREFIX}.manifest.json"
    digest_path = output_dir / f"{ARTIFACT_NAME}.sha256"
    artifact_path.write_bytes(built.content)
    manifest_path.write_bytes(built.manifest)
    digest = sha256_bytes(built.content)
    digest_path.write_text(f"{digest}  {ARTIFACT_NAME}\n", encoding="utf-8")

    print("Radar V2 public production artifact: PASS")
    print(f"Runtime files: {len(built.files)}")
    print(f"Artifact SHA-256: {digest}")
    try:
        displayed_manifest_path = manifest_path.relative_to(V2_ROOT)
    except ValueError:
        displayed_manifest_path = manifest_path
    print(f"Manifest: {displayed_manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
