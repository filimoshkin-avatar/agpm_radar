"""Count what `design/DESIGN-SYSTEM.md` forbids, and hold the number.

A ratchet, not a wall. The design system arrived on 2026-08-25 against a
front-end that already broke it, so failing outright would have meant either
refusing the system or rewriting production the same day. Instead the debt is
written down: more than the recorded number fails, and fewer than it also fails
- because a debt that quietly shrinks is a number nobody updates, and the next
regression hides inside the slack.

The redesign rewrites both files, so these should reach zero and stay there.

Not checked here, deliberately: colours outside the palette. Forty-two of the
ninety-three hex values in `styles.css` are outside it today, and most are the
old scheme the redesign replaces wholesale - a number that large teaches
nothing until it is small. Add it when the redesign lands.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final

WEB: Final = Path(__file__).resolve().parents[1] / "apps" / "web"

#: What the current front-end owes the design system. Lower these as the
#: redesign removes them; the gate insists you do.
DEBT: Final = {
    "gradients": 6,
    "scrollIntoView": 6,
    "banned-fonts": 0,
    "emoji": 0,
}

#: `Inter` also lives inside `setInterval`, which is why this looks at
#: `font-family` declarations rather than at the whole file. The first count
#: made that mistake and reported four violations that did not exist.
_FONT_FAMILY: Final = re.compile(r"font-family\s*:[^;{}\n]*", re.I)
_BANNED_FONT: Final = re.compile(r"\b(Inter|Roboto|Arial)\b")
_GRADIENT: Final = re.compile(r"\b(?:linear|radial|conic)-gradient\s*\(")
_SCROLL_INTO_VIEW: Final = re.compile(r"\bscrollIntoView\b")
#: True emoji blocks, plus the variation selector that turns an older symbol
#: into one. Deliberately NOT the Dingbats and Miscellaneous Symbols blocks:
#: those hold ← → ✓ ✕, which this front-end uses as typography - a check mark
#: after «скопировано», a close glyph on a button. A first version of this rule
#: swept them in and reported two violations that were not violations; the ban
#: in the design system sits beside gradients and Arial and means decoration,
#: not every non-Latin glyph.
_EMOJI: Final = re.compile("[\U0001f300-\U0001faff]|\ufe0f")


def _read(name: str) -> str:
    path = WEB / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def count() -> dict[str, int]:
    css, js, html = _read("styles.css"), _read("app.mjs"), _read("index.html")
    families = _FONT_FAMILY.findall(css) + _FONT_FAMILY.findall(js)
    return {
        "gradients": len(_GRADIENT.findall(css)) + len(_GRADIENT.findall(js)),
        "scrollIntoView": len(_SCROLL_INTO_VIEW.findall(js)),
        "banned-fonts": sum(1 for family in families if _BANNED_FONT.search(family)),
        "emoji": len(_EMOJI.findall(css + js + html)),
    }


def main() -> int:
    found = count()
    worse = {rule: (found[rule], DEBT[rule]) for rule in DEBT if found[rule] > DEBT[rule]}
    better = {rule: (found[rule], DEBT[rule]) for rule in DEBT if found[rule] < DEBT[rule]}
    for rule in sorted(DEBT):
        print(f"  {rule:16} {found[rule]:>3}  записано {DEBT[rule]:>3}")
    if worse:
        for rule, (now, owed) in sorted(worse.items()):
            print(
                f"design rule «{rule}» broken {now} times, {owed} recorded:"
                f" see design/DESIGN-SYSTEM.md",
                file=sys.stderr,
            )
        return 1
    if better:
        for rule, (now, owed) in sorted(better.items()):
            print(
                f"design rule «{rule}» is down to {now} from {owed} - lower DEBT"
                f" in {Path(__file__).name} so the gain is held",
                file=sys.stderr,
            )
        return 1
    print("Radar V2 design-system debt: HELD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
