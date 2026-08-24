# Vendored frontend dependencies

The web app of Radar V2 is dependency-free by architecture decision; this
directory is the one exception, and each file here carries its full provenance.
Nothing in this directory is wired into `index.html` or the production artifact
yet — Increment 1 of the «Связи» plan does that, after the ADR.

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

- `eval(` — 0 occurrences; `new Function` — 0 occurrences;
- URL literals — 3, all inside the MIT license header
  (`en.wikipedia.org/wiki/MIT_License`, `engelschall.com`,
  `opensource.org/licenses/MIT`); no network endpoints;
- secret-shaped content — none: no private keys, `AKIA…`, `gh…_`, `sk-…`
  and no `[0-9]{8,10}:[A-Za-z0-9_-]{30,}` (the scanner's five patterns);
- the workspace isolation scan (`v2/tools/check_isolation.py`) passes with the
  file present: it is valid UTF-8, needs no `ALLOWED_BINARY_ASSETS` entry, and
  the remote-URL check applies to `.html` files only;
- all three project gates pass with the file in the tree
  (`kx` verify, `kx` verify_migrations, `v2` verify — the artifact stays at
  26 runtime files until `packages/deployment/artifacts.py` learns the
  `vendor/` path, which is Increment 1 work).

The ADR naming this the first third-party frontend dependency, and the wiring
(classic `<script>` before `app.mjs`, no module loader, no CDN), belong to
Increment 1.
