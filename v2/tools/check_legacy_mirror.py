"""Fail closed when the Git Legacy scripts differ from Project Manager's runtime copy."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

FILES = (
    "agpm_radar_collect.py",
    "agpm_radar_daily.sh",
    "agpm_radar_report.py",
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-scripts", required=True, type=Path)
    parser.add_argument("--runtime-scripts", required=True, type=Path)
    args = parser.parse_args()
    differences = [
        name
        for name in FILES
        if not (args.repository_scripts / name).is_file()
        or not (args.runtime_scripts / name).is_file()
        or _digest(args.repository_scripts / name) != _digest(args.runtime_scripts / name)
    ]
    if differences:
        raise SystemExit("Legacy runtime mirror drift: " + ", ".join(differences))
    print("Legacy runtime mirror: PASS (3 files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
