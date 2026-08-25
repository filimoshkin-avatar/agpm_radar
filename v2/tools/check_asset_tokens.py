"""The cache token must be the file, not a date somebody remembered to change.

Caddy serves `/assets/app.mjs` and `/assets/styles.css` as `public,
max-age=31536000, immutable`. Immutable means the browser does not revalidate -
it will not so much as ask - so the only thing that can hand a returning reader
new code is a different URL. That URL differs by `?v=` in `index.html`, and
`index.html` is `no-store`.

Written down twice in AGENTS.md and broken twice anyway. In August 2026 the
styles token stood through four releases and four hundred lines of new CSS never
reached anybody who had been to the site before. On 2026-08-25 both tokens stood
through five releases: the redesign shipped, and then the widgets, the voice
input, the counts, the chain mark and a whole UX round were served to the server
and to nobody else. Every browser test passed, because a fresh browser has no
cache to be stale.

A date in a token is a promise to remember. This is the same rule with the
remembering taken out: the token IS the first twelve hex of the file's SHA-256,
so it cannot fail to change when the file changes, and when it is wrong this
check prints the exact string to paste.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Final

WEB: Final = Path(__file__).resolve().parents[1] / "apps" / "web"

#: The two assets Caddy freezes for a year. Anything else added to that matcher
#: belongs here too - and adding a path to the matcher is a Caddy change.
FROZEN: Final = ("app.mjs", "styles.css")

#: How much of the digest goes into the URL. Twelve hex is 48 bits: collisions
#: are not the risk here, a token nobody changed is.
TOKEN_LENGTH: Final = 12


def token_for(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:TOKEN_LENGTH]


def referenced(html: str, name: str) -> str | None:
    found = re.search(rf"/assets/{re.escape(name)}\?v=([^\"'&]+)", html)
    return found.group(1) if found else None


def main() -> int:
    html_path = WEB / "index.html"
    html = html_path.read_text(encoding="utf-8")
    wrong: list[tuple[str, str | None, str]] = []
    for name in FROZEN:
        path = WEB / name
        if not path.exists():
            print(f"{name}: missing", file=sys.stderr)
            return 1
        want = token_for(path)
        got = referenced(html, name)
        print(f"  {name:12} ?v={got or '(none)'}  ожидается {want}")
        if got != want:
            wrong.append((name, got, want))
    if wrong:
        print(file=sys.stderr)
        for name, got, want in wrong:
            print(
                f"cache token for {name} is «{got}», the file hashes to «{want}»:"
                f" a returning reader is still being served the old file."
                f" Set it in index.html:\n"
                f'    <... href="/assets/{name}?v={want}">',
                file=sys.stderr,
            )
        return 1
    print("Radar V2 asset cache tokens: match the files they name")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
