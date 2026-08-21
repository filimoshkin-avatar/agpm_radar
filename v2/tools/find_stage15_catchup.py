"""Select the oldest recent Legacy issue that has no completed V2 dual-run report."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exports-root", required=True, type=Path)
    parser.add_argument("--runs-root", required=True, type=Path)
    parser.add_argument("--through", required=True)
    parser.add_argument(
        "--not-before",
        required=True,
        help="Earliest date the daily contour owns; older Legacy issues are never caught up.",
    )
    parser.add_argument("--lookback-days", type=int, default=7)
    args = parser.parse_args()
    through = date.fromisoformat(args.through)
    not_before = date.fromisoformat(args.not_before)
    for offset in range(args.lookback_days - 1, -1, -1):
        candidate = through - timedelta(days=offset)
        if candidate < not_before:
            continue
        issue_date = candidate.isoformat()
        if (args.exports_root / f"{issue_date}.json").is_file() and not (
            args.runs_root / issue_date / "combined-report.json"
        ).is_file():
            print(issue_date)
            return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
