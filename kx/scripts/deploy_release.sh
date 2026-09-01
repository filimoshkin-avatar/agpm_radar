#!/usr/bin/env bash
# Ship the working tree's committed HEAD to Local Ru as a new release.
#
# This existed as a recipe run by hand until 2026-08-22, and the recipe had a hole:
# it rewrote the release id in /usr/local/sbin/kxrun and not in
# /etc/radar-kx/worker.env, so every unit that reads the env file - the ingest timer
# and now the orchestrator - stamped its rows with whichever release was current the
# last time somebody edited that file by hand. Nothing was mislabelled in the end,
# because the timer happened to be stopped, but the store's whole claim is that a row
# says which code wrote it. Automating the recipe is how that stops depending on
# remembering.
set -euo pipefail

HOST="${RADAR_KX_HOST:-root@radar.agpm.space}"
KEY="${RADAR_KX_SSH_KEY:-/root/.ssh/local_ru_admin}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

say() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] FAIL: %s\n' "$*" >&2; exit 1; }

cd "$REPO_ROOT"

# A migration moves the database out from under anything already running:
# `require_schema` is a hard gate (defect D2) and a long batch loaded the old
# constant at start. On 2026-08-22 applying migration 010 killed an extraction
# pass 232 fragments into 1098. Deploying is safe; applying a migration is not,
# and the runbook for one starts by stopping the orchestrator.
# --state=active does not match a running oneshot: it sits in `activating` for
# its whole life. The first version of this guard used `active` alone and let a
# migration kill a canon extraction pass while claiming to watch for exactly that.
if ssh -i "$KEY" -o BatchMode=yes "$HOST" \
       "systemctl list-units --state=active,activating --no-legend 'radar-kx-orchestrator@*' \
        | grep -q ." 2>/dev/null; then
    say "NOTE: an orchestrator batch is running; a migration would kill it mid-pass"
fi
[[ -z "$(git status --porcelain kx)" ]] || die "kx/ has uncommitted changes"
commit="$(git rev-parse HEAD | cut -c1-12)"
release="radar_kx_release_$(date -u +%Y%m%d)_${commit}"
say "release $release"

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
# The slice documents travel with the release: the editor serves them for
# reading, and the host has no other copy of them.
git archive HEAD kx docs | tar -x -C "$staging"
mv "$staging/docs" "$staging/kx/docs"
tar -czf "$staging/$release.tar.gz" -C "$staging/kx" .
scp -i "$KEY" -o BatchMode=yes "$staging/$release.tar.gz" "$HOST:/tmp/"

ssh -i "$KEY" -o BatchMode=yes "$HOST" "REL='$release' bash -s" <<'REMOTE'
set -euo pipefail
DST="/opt/radar-kx/releases/$REL"
OLD="$(readlink -f /opt/radar-kx/current)"
rm -rf "$DST" && mkdir -p "$DST"
tar -xzf "/tmp/$REL.tar.gz" -C "$DST"
# Gitignored and carried forward across releases.
[[ -f "$OLD/deploy/radar-kx.env" ]] && cp -a "$OLD/deploy/radar-kx.env" "$DST/deploy/"
cd "$DST"
find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256
chmod -R u=rwX,go=rX "$DST"
[[ -f "$DST/deploy/radar-kx.env" ]] && chmod 0600 "$DST/deploy/radar-kx.env"
ln -sfn "releases/$REL" /opt/radar-kx/current.new && mv -T /opt/radar-kx/current.new /opt/radar-kx/current
# The release id lives in four places and all four must move together. This
# said "three" and updated two of them: `kb.env` was left out, and on
# 2026-08-25 the knowledge-base service was stamping its rows with
# radar_kx_release_20260823 while the workers were two releases ahead. Same
# hole this script was written to close, one file over.
sed -i "s#radar_kx_release_[0-9]\{8\}_[0-9a-f]\{12\}#$REL#g" /usr/local/sbin/kxrun
sed -i "s#^RADAR_KX_RELEASE_ID=.*#RADAR_KX_RELEASE_ID=$REL#" /etc/radar-kx/worker.env
sed -i "s#^RADAR_KX_RELEASE_ID=.*#RADAR_KX_RELEASE_ID=$REL#" /etc/radar-kx/kb.env
rm -f "/tmp/$REL.tar.gz"
echo "current   -> $(readlink /opt/radar-kx/current)"
echo "kxrun     -> $(grep -o 'radar_kx_release_[0-9a-f_]*' /usr/local/sbin/kxrun | head -1)"
echo "worker.env-> $(grep '^RADAR_KX_RELEASE_ID=' /etc/radar-kx/worker.env | cut -d= -f2)"
echo "kb.env    -> $(grep '^RADAR_KX_RELEASE_ID=' /etc/radar-kx/kb.env | cut -d= -f2)"
REMOTE
say "deployed"
