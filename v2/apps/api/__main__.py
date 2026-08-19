"""Preflight or serve the loopback-only Radar V2 application."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packages.storage.sqlite_profile import assert_sqlite_runtime

from apps.api import ActiveDatabaseManager, RadarApi, RadarApplication, status_payload
from apps.api.http_server import serve


def main(argv: list[str] | None = None) -> int:
    """Print build identity or serve when explicit roots are supplied."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--active-root", type=Path)
    parser.add_argument("--gazette-root", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args(argv)
    assert_sqlite_runtime()
    if arguments.active_root is not None:
        if arguments.gazette_root is None:
            parser.error("--gazette-root is required with --active-root")
        manager = ActiveDatabaseManager(arguments.active_root)
        application = RadarApplication(
            RadarApi(manager),
            web_root=Path(__file__).resolve().parents[1] / "web",
            gazette_root=arguments.gazette_root,
        )
        try:
            serve(application, host=arguments.host, port=arguments.port)
        finally:
            manager.close()
        return 0
    print(json.dumps(status_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
