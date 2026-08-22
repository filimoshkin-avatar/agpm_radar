#!/usr/bin/env python3
"""Inventory the AgPM file wiki. Read-only: it opens files and writes only its own report.

    python3 scripts/wiki_inventory.py \
        --knowledge-root /root/.openclaw-projectmanager/workspace/knowledge \
        --output wiki-inventory.json

``--knowledge-root`` is the directory that holds ``agpm/`` and ``agpm-radar/``; pass
``--root name=path`` instead to inventory a different pair.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from radar_kx.wiki_inventory import SECTION_ALIASES, build_register


def _roots(args: argparse.Namespace) -> dict[str, Path]:
    if args.root:
        roots: dict[str, Path] = {}
        for item in args.root:
            name, _, path = item.partition("=")
            if not name or not path:
                raise SystemExit(f"--root expects name=path, got {item!r}")
            roots[name] = Path(path)
        return roots
    base = args.knowledge_root
    return {"agpm": base / "agpm", "agpm-radar": base / "agpm-radar"}


def _summary(register: dict[str, Any]) -> str:
    inventory = register["inventory"]
    totals = inventory["totals"]
    conventions = inventory["pageConventions"]
    evidence = inventory["evidencePosture"]
    lines = [
        "AgPM wiki inventory",
        "",
        f"  markdown pages          {totals['markdownPages']:>6}"
        f"   ({totals['bytesMarkdown'] / 1e6:.1f} MB)",
        f"  other assets            {totals['nonMarkdownAssets']:>6}"
        f"   ({totals['bytesAssets'] / 1e6:.1f} MB)",
        f"  authored pages          {totals['authoredPages']:>6}"
        f"   ({totals['authoredWords']} words)",
        f"  atomic claim candidates {totals['atomicClaimCandidates']:>6}",
        "",
        "  by layer:",
    ]
    for layer, count in inventory["byLayer"].items():
        lines.append(f"    {layer:<22} {count:>5}")
    lines.append("")
    lines.append("  SCHEMA.md page conventions, over authored pages:")
    for name, _ in SECTION_ALIASES:
        count = conventions["sectionCoverage"][name]
        share = count / conventions["authoredPages"] if conventions["authoredPages"] else 0.0
        lines.append(f"    {name:<22} {count:>5}   {share:5.0%}")
    lines.append(
        f"    fully conformant       {len(conventions['fullyConformantPages']):>5}"
        f"   of {conventions['authoredPages']}"
    )
    lines.append(
        f"    unmapped H2 headings   {conventions['unmappedLevelTwoHeadings']:>5}"
        f"   of {conventions['distinctLevelTwoHeadings']} distinct"
    )
    lines.append("")
    lines.append(
        f"  pages citing a source   {evidence['pagesCitingSources']:>6}"
        f"   of {conventions['authoredPages']}"
    )
    lines.append(f"  broken internal links   {len(inventory['linkGraph']['brokenLinks']):>6}")
    lines.append(f"  empty directories       {len(inventory['emptySectionDirectories']):>6}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(prog="wiki_inventory")
    parser.add_argument(
        "--knowledge-root",
        type=Path,
        default=Path("/root/.openclaw-projectmanager/workspace/knowledge"),
    )
    parser.add_argument("--root", action="append", help="name=path, repeatable")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    register = build_register(_roots(args))
    if args.output is not None:
        args.output.write_text(
            json.dumps(register, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if not args.quiet:
        print(_summary(register))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
