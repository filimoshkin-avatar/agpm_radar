"""Generate V2-native period analyses for one already published anchor issue."""

from __future__ import annotations

import argparse
from pathlib import Path

from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.storage.safe_files import atomic_write_new

from tools.v2_period_analysis import generate_period


def generate(*, database: Path, issue_date: str, output: Path) -> JsonObject:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    seven = generate_period(
        database=database,
        anchor=issue_date,
        period="7d",
        artifacts_root=output / "artifacts",
    )
    thirty = generate_period(
        database=database,
        anchor=issue_date,
        period="30d",
        artifacts_root=output / "artifacts",
        previous=seven["theses"],  # type: ignore[arg-type]
    )
    result: JsonObject = {"issueDate": issue_date, "periods": {"7d": seven, "30d": thirty}}
    atomic_write_new(output / "result.json", canonical_json_line(result), mode=0o600)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--issue-date", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(
        canonical_json_line(
            generate(database=args.database, issue_date=args.issue_date, output=args.output)
        ).decode(),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
