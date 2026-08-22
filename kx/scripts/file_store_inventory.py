#!/usr/bin/env python3
"""Inventory a Project Manager file store, for comparison against KX.

Runs on the control host, where the files are. Emits identifiers and counts only:
canonical URL, how much text the store holds, and the status it recorded. No text
leaves, and nothing is written except the inventory itself.

    python3 file_store_inventory.py --knowledge-root .../agpm-radar \
        --scope source_fulltext --output inventory.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def source_fulltext(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted((root / "data" / "source-fulltext").glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            continue
        url = value.get("canonical_url") or value.get("url")
        if not url:
            continue
        text = value.get("text")
        entries.append(
            {
                "canonicalUrl": str(url),
                "textChars": len(text) if isinstance(text, str) else 0,
                "status": str(value.get("status") or "unknown"),
            }
        )
    return entries


def discovery_registry(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with (root / "data" / "materials.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            url = value.get("canonical_url") or value.get("url")
            if not url:
                continue
            entries.append(
                {
                    "canonicalUrl": str(url),
                    "textChars": 0,
                    "status": str(value.get("perimeter") or "unknown"),
                }
            )
    return entries


BUILDERS = {"source_fulltext": source_fulltext, "discovery_registry": discovery_registry}


def main() -> int:
    parser = argparse.ArgumentParser(prog="file_store_inventory")
    parser.add_argument("--knowledge-root", type=Path, required=True)
    parser.add_argument("--scope", choices=sorted(BUILDERS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    entries = BUILDERS[args.scope](args.knowledge_root)
    args.output.write_text(
        json.dumps(
            {
                "scope": args.scope,
                "generatedAt": datetime.now(UTC).isoformat(),
                "root": str(args.knowledge_root),
                "entries": entries,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"scope": args.scope, "entries": len(entries)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
