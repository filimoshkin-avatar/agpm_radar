# Vendored frontend dependencies

The web app of Radar V2 is dependency-free by architecture decision; this
directory is the one exception, and each file here carries its full provenance.
The exception is argued in `docs/adr/0009-first-vendored-frontend-dependency-cytoscape.md`.

## cytoscape.3.30.4.min.js

| | |
|---|---|
| Package | `cytoscape` 3.30.4 |
| Licence | MIT (license header kept in the file; upstream — https://github.com/cytoscape/cytoscape.js) |
| Source | https://unpkg.com/cytoscape@3.30.4/dist/cytoscape.min.js |
| SHA-256 | `1bb5340e549511e111b31e5684872c949ad33d40ea5dba0ad8e7d90c62c7b3b9` |
| Size | 373 734 bytes |
| Fetched | 2026-08-24 |
| Runtime dependencies | none (`package.json` of 3.30.4 declares no `dependencies`) |

Checks performed before accepting the file (2026-08-24, Increment 0):

- `eval(` — 0 occurrences; `new Function` — 0 occurrences. Note what this does
  **not** say: there is one `Function("return this")()`, the lodash idiom for
  finding the global object. It is short-circuited in a browser (`freeSelf` is
  truthy) and never reached, which matters because this site's CSP is
  `script-src 'self'` with no `'unsafe-eval'` and the call would otherwise
  throw. The original check grepped `new Function` and missed it;
- URL literals — 3, all inside the MIT license header
  (`en.wikipedia.org/wiki/MIT_License`, `engelschall.com`,
  `opensource.org/licenses/MIT`); no network endpoints;
- secret-shaped content — none: no private keys, `AKIA…`, `gh…_`, `sk-…`
  and no `[0-9]{8,10}:[A-Za-z0-9_-]{30,}` (the scanner's five patterns);
- the workspace isolation scan (`v2/tools/check_isolation.py`) passes with the
  file present: it is valid UTF-8, needs no `ALLOWED_BINARY_ASSETS` entry, and
  the remote-URL check applies to `.html` files only;
- all three project gates pass with the file in the tree
  (`kx` verify, `kx` verify_migrations, `v2` verify).

## How it is wired (Increment 1)

`packages/deployment/artifacts.py` carries the path in `WEB_PATHS`, so the file
travels inside the production artifact as a runtime file — 27 of them. It loads
from a classic `<script>` before `app.mjs`: no module loader, no CDN, no build
step. The version is in the filename, so a future upgrade changes the path and
the year-long `immutable` cache header cannot serve a stale library.

Caddy must route `/assets/vendor/*` into the assets handler. Until 2026-08-24
the matcher listed exact paths only and this file fell through to the site's
`respond "Not Found" 404`.

The drawing is never the interface: the list is primary and complete for every
centre, and the canvas is gated on this library being present. Where it is
absent — an old browser, the console smoke — the reader loses the picture and
nothing else.
