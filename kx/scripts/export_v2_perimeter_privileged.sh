#!/usr/bin/env bash
# Export the active Radar V2 perimeter to a file KX can read.
#
# Slice 2.2. Radar V2's content directory is closed to everyone but the API's own
# user, and this is deliberately not changed: the coupling between the two systems
# is one-way and read-only, and V2 does not know KX exists (ADR-0005 §18). So this
# one step runs privileged - as `ExecStartPre=+` inside the poll unit - and hands
# the artifact to `radar_kx` through a file it owns nothing of.
#
# The export itself opens the release database with an immutable URI and uses the
# standard library only, so it cannot write to, lock or disturb the running API.
set -euo pipefail

CONTENT_ROOT="${RADAR_V2_CONTENT_ROOT:-/var/lib/radar-v2/content}"
RELEASE="${RADAR_KX_RELEASE_DIR:-/opt/radar-kx/current}"
TARGET_DIR=/var/lib/radar-kx/perimeter
TARGET="$TARGET_DIR/latest.json"

[[ -r "$CONTENT_ROOT/active.json" ]] || {
    echo "[perimeter-poll] $CONTENT_ROOT/active.json is not readable" >&2
    exit 1
}

# The directory is systemd's StateDirectory; only the artifact is ours to place.
[[ -d "$TARGET_DIR" ]] || { echo "[perimeter-poll] $TARGET_DIR is missing" >&2; exit 1; }
/usr/bin/python3 "$RELEASE/scripts/export_v2_perimeter.py" \
    --content-root "$CONTENT_ROOT" --output "$TARGET.new"
chown radar_kx:radar_kx "$TARGET.new"
chmod 0640 "$TARGET.new"
mv -f "$TARGET.new" "$TARGET"
