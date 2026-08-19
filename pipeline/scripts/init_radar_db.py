#!/usr/bin/env python3
"""Initialize the Radar SQLite database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from radar_paths import DB_PATH, MIGRATIONS_DIR, ensure_dirs


def apply_migration(conn: sqlite3.Connection, path: Path) -> None:
    version = path.name
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version TEXT PRIMARY KEY,
          applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    row = conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone()
    if row:
        return
    conn.executescript(path.read_text(encoding="utf-8"))
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (version,))


def main() -> int:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            apply_migration(conn, path)
        conn.commit()
    finally:
        conn.close()
    print(f"Initialized {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
