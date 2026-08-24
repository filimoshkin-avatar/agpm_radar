# ADR-0009: The first third-party frontend dependency — Cytoscape, vendored

Date: 2026-08-24

Status: proposed 2026-08-24, awaiting the owner

## Context

Radar V2's web app has been dependency-free by architecture decision since ADR-0003, and the gate
enforces it: `v2/scripts/verify.sh` reports «browser module dependency-free» on every run. The
«Связи» plan needs a neighbourhood drawn as a picture, and the increment-0 question was whether that
picture is worth breaking a standing decision for.

The previous drawing was hand-rolled SVG: a radial layout computed in `agentGraphSvg`, one hop from
one centre. It worked because one hop around one node already has a shape. It stopped working the
moment the neighbourhood had to be navigable — no hit-testing beyond `<g>` elements, no layout that
survives forty nodes, no panning, no way to tell a reader that thirty of a thousand statements are
drawn.

Three options were on the table.

1. **Keep hand-rolled SVG and grow it.** Cheapest today. But a layout engine, hit-testing, panning
   and zooming are exactly the parts that look small and are not; we would be writing a graph
   library inside a file that also renders a gazette.
2. **A CDN `<script>`.** Rejected outright: the site's CSP is `default-src 'self'; script-src 'self'`
   with no `'unsafe-eval'`, `connect-src 'self'`, and the base's whole claim is that it does not
   depend on somebody else's uptime or somebody else's bytes changing under it.
3. **Vendor the library into the repository.** Chosen.

## Decision

### 1. Cytoscape 3.30.4 is vendored, not fetched

The file lives at `v2/apps/web/vendor/cytoscape.3.30.4.min.js`, is committed, travels inside the
production artifact as a runtime file (`WEB_PATHS`, 27 files), and is loaded by a classic `<script>`
tag before `app.mjs`. No module loader, no CDN, no build step. The version is in the filename, so
replacing a version changes the path and the year-long `immutable` cache header cannot serve a stale
library.

### 2. Provenance is recorded and re-checkable

`v2/apps/web/vendor/README.md` carries the source URL, the licence, the byte count and the SHA-256.
The file is byte-identical to `https://unpkg.com/cytoscape@3.30.4/dist/cytoscape.min.js`
(`1bb5340e549511e111b31e5684872c949ad33d40ea5dba0ad8e7d90c62c7b3b9`, 373 734 bytes), verified by
download and comparison, not by trusting the note.

### 3. What the dynamic-code check actually found

The accepted claim is **not** «no dynamic code». `eval(` and `new Function` do not occur, but the
bundle contains one `Function("return this")()` — the lodash idiom for finding the global object. It
is short-circuited in a browser: `freeSelf` is truthy, and the call is never reached. Under this
site's CSP a call *would* throw, so this is worth stating rather than leaving to a future reader to
rediscover. The check that missed it grepped `new Function`; the pattern was too narrow.

### 4. The drawing is never the interface

The list is primary for every centre and complete on its own: real buttons, real focus, groups,
counts, and an `aria-live` line saying how much of the neighbourhood is held back. The canvas is an
addition, gated on the library being present, the screen being wide enough, and — for a subject —
on the reader asking for it. Where Cytoscape is absent the reader loses nothing but the picture.

This is what makes the dependency acceptable: it is not load-bearing. If it were removed tomorrow,
«Связи» would still work.

### 5. Serving

`/assets/vendor/*` must be routed by Caddy into the assets handler. Until 2026-08-24 the matcher
listed exact paths and the vendored file fell through to the site's `respond "Not Found" 404`; the
matcher now carries `/assets/vendor/*`.

## Consequences

- The gate's «dependency-free» line now means «dependency-free except one vendored, pinned,
  hash-recorded file», and the README is where that exception is written down.
- Upgrading is a deliberate act: new filename, new hash in the README, new comparison against
  upstream. There is no automatic update path, by design.
- A second vendored dependency is not covered by this ADR. The next one needs its own argument.
