"""Print the Stage 2 application identity after validating the SQLite runtime."""

from __future__ import annotations

import json

from packages.storage import assert_sqlite_runtime

from apps.api import status_payload


def main() -> int:
    """Run the dependency-free Stage 2 preflight."""
    assert_sqlite_runtime()
    print(json.dumps(status_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
