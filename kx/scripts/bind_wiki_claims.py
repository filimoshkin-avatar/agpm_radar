#!/usr/bin/env python3
"""Find candidate evidence in KX for the atomic claims of the AgPM wiki.

First half of slice 2.5, and deliberately only the first half. What this produces
is a **retrieval report**, not evidence: a lexical hit says a passage uses similar
language, which is not the same as a passage supporting a claim. ADR-0004 admits
nothing to ``claim_evidence`` without an exact span and an accepted claim, and
neither exists yet - the tables arrive with slice 2.6.

What the report is for is the number the plan asks for and nobody has: how much of
what the wiki asserts the store can support **at all**. Slice 1.5 measured the
other side - 27 of 63 authored pages cite nothing - and this measures whether the
citation could exist if somebody looked.

    python3 bind_wiki_claims.py --inventory wiki-inventory.json --output report.json

Runs where the text is. Claims are Radar's own writing and travel freely; what
comes back with them is a quotation-length snippet per hit.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from radar_kx.config import Settings
from radar_kx.database import Database

#: Layers whose pages assert things in Radar's voice. Raw extracts are somebody
#: else's text and are not ours to bind.
AUTHORED_LAYERS = frozenset({"synthesis_page", "source_note", "radar_overview", "monthly_summary"})

#: A claim shorter than this is a fragment - "See below", a bare link - and
#: searching for it produces noise, not candidates.
MIN_CLAIM_CHARS = 40

#: Scopes searched per claim, in order. The canon is where a claim about the AgPM
#: model should land; the perimeter is where a claim about the market should.
SCOPES = ("canon", "current")


def _settings(dsn: str) -> Settings:
    base = Settings.from_environment()
    return Settings(**{**dataclasses.asdict(base), "dsn": dsn})


def bind(
    database: Database, inventory: dict[str, Any], *, limit: int, match: str
) -> dict[str, Any]:
    pages = [
        page for page in inventory["pages"] if page["layer"] in AUTHORED_LAYERS and page["claims"]
    ]
    results: list[dict[str, Any]] = []
    per_scope: Counter[str] = Counter()
    bound = 0
    total = 0
    skipped = 0

    for page in pages:
        for claim in page["claims"]:
            text = str(claim["text"])
            if len(text) < MIN_CLAIM_CHARS:
                skipped += 1
                continue
            total += 1
            hits: list[dict[str, Any]] = []
            for scope in SCOPES:
                for hit in database.search(text, scope=scope, limit=limit, match=match):
                    hits.append({**hit.as_json(), "scope": scope})
                    per_scope[scope] += 1
            hits.sort(key=lambda item: float(item["rrfScore"]), reverse=True)
            if hits:
                bound += 1
            results.append(
                {
                    "page": page["relativePath"],
                    "section": claim["section"],
                    "line": claim["line"],
                    "claim": text,
                    "candidates": hits[:limit],
                }
            )

    by_page = Counter(item["page"] for item in results)
    unsupported = Counter(item["page"] for item in results if not item["candidates"])
    return {
        "summary": {
            "pages": len(pages),
            "claims": total,
            "claimsTooShortToSearch": skipped,
            "claimsWithACandidate": bound,
            "claimsWithNoCandidate": total - bound,
            "candidateHitsByScope": dict(sorted(per_scope.items())),
            "match": match,
            "limitPerScope": limit,
        },
        "pages": [
            {
                "page": page,
                "claims": by_page[page],
                "claimsWithNoCandidate": unsupported.get(page, 0),
            }
            for page in sorted(by_page)
        ],
        "claims": results,
        "caveat": (
            "A candidate is a lexical hit, not evidence. ADR-0004 admits nothing to "
            "claim_evidence without an exact span bound to an accepted claim, and both "
            "arrive with slice 2.6. This report says only where somebody should look."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="bind_wiki_claims")
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--match", default="any", choices=("all", "any"))
    parser.add_argument("--dsn", default=os.environ.get("RADAR_KX_DSN", ""))
    args = parser.parse_args()

    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    report = bind(Database(_settings(args.dsn)), inventory, limit=args.limit, match=args.match)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
