#!/usr/bin/env bash
# Build one Radar V2 application release from a clean worktree at a commit and
# install its api and web roles on Local Ru, in that order: the service first,
# the front second, because a new front against an old service asks for
# endpoints the service does not have (AGENTS.md, «Выкат фронта»).
#
# The recipe lived in memory and in hands until 2026-09-05, and a recipe run by
# hand drifts: on 2026-08-27 the api tree went out root:root and world-readable.
# Every step below is what the runbook says, once, checked, with the rollback
# commands printed at the end.
#
#   v2/scripts/deploy_application_release.sh <commit> [--web-only]
#
# Needs: a clean tracked commit (the builder refuses anything else), the v2
# venv, root SSH to Local Ru with /root/.ssh/local_ru_admin.
set -euo pipefail

commit_ref="${1:?usage: deploy_application_release.sh <commit> [--web-only]}"
web_only="${2:-}"
# Опечатка в этом слове означала бы «выкатывай всё», то есть рестарт продовой
# службы там, где просили только фронт.
if [[ -n "$web_only" && "$web_only" != "--web-only" ]]; then
  printf '[deploy] FAIL: unknown argument: %s\n' "$web_only" >&2
  exit 2
fi
HOST="${RADAR_V2_HOST:-root@radar.agpm.space}"
KEY="${RADAR_V2_SSH_KEY:-/root/.ssh/local_ru_admin}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="$REPO/v2/.venv/bin/python"

say() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] FAIL: %s\n' "$*" >&2; exit 1; }

full="$(git -C "$REPO" rev-parse --verify "${commit_ref}^{commit}")"
short="${full:0:7}"
rel="app_release_$(date -u +%Y%m%d)_${short}"
created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
work="$(mktemp -d "/tmp/radar-v2-release-${rel}-XXXXXX")"
chmod 0700 "$work"
say "release $rel from $full"
say "work dir $work (retained)"

# A fresh detached worktree is a completely clean tracked tree by construction;
# the builder re-checks that and refuses untracked files.
git -C "$REPO" worktree add --detach "$work/source" "$full" >/dev/null
trap 'git -C "$REPO" worktree remove --force "$work/source" >/dev/null 2>&1 || true' EXIT
(
  cd "$work/source/v2"
  PYTHONPATH="$work/source/v2" "$PYTHON" tools/build_application_release.py \
    --application-release-id "$rel" \
    --git-commit "$full" \
    --created-at "$created" \
    --output-dir "$work/dist"
)
(cd "$work/dist" && sha256sum -c radar-v2-application-release.tar.gz.sha256 >/dev/null)
package_sha="$(cut -d' ' -f1 "$work/dist/radar-v2-application-release.tar.gz.sha256")"
# До первой отправки: собранный `index.html` называет собранные ассеты. Правило
# ломали трижды, последний раз пятью релизами подряд, и каждый раз его замечали
# после выката. Здесь ещё ничего не выкачено.
(cd "$work/source/v2" && python3 tools/check_asset_tokens.py >/dev/null) \
  || die "собранный index.html называет не те ассеты — см. tools/check_asset_tokens.py"
app_token="$(sha256sum "$work/source/v2/apps/web/app.mjs" | cut -c1-12)"
css_token="$(sha256sum "$work/source/v2/apps/web/styles.css" | cut -c1-12)"
say "asset tokens: app.mjs $app_token, styles.css $css_token"
mkdir -p "$work/stage"
tar -xzf "$work/dist/radar-v2-application-release.tar.gz" -C "$work/stage"
(cd "$work/stage/radar-v2-application-release" && sha256sum -c checksums.sha256 >/dev/null)
printf '{"applicationReleaseId":"%s","gitCommit":"%s","packageSha256":"%s"}\n' \
  "$rel" "$full" "$package_sha" > "$work/APPLICATION-RELEASE.json"
say "package sha256 $package_sha"

ssh -i "$KEY" -o BatchMode=yes "$HOST" "install -d -m 0750 -o root -g radar-v2-deploy /var/lib/radar-v2/incoming/application/$rel"
scp -q -i "$KEY" -o BatchMode=yes \
  "$work/dist/radar-v2-application-release.tar.gz" \
  "$work/dist/radar-v2-application-release.tar.gz.sha256" \
  "$work/APPLICATION-RELEASE.json" \
  "$HOST:/var/lib/radar-v2/incoming/application/$rel/"

ssh -i "$KEY" -o BatchMode=yes "$HOST" \
  "REL='$rel' WEB_ONLY='$web_only' APP_TOKEN='$app_token' CSS_TOKEN='$css_token' bash -s" <<'REMOTE'
set -euo pipefail
say() { printf '[remote] %s\n' "$*"; }
die() { printf '[remote] FAIL: %s\n' "$*" >&2; exit 1; }
IN="/var/lib/radar-v2/incoming/application/$REL"
cd "$IN"
sha256sum -c radar-v2-application-release.tar.gz.sha256 >/dev/null
chmod 0600 radar-v2-application-release.tar.gz APPLICATION-RELEASE.json
mkdir -p stage && tar -xzf radar-v2-application-release.tar.gz -C stage
cd stage/radar-v2-application-release && sha256sum -c checksums.sha256 >/dev/null && cd "$IN"

# Every file of a role is compared with the role's own MANIFEST.json before it
# can become a release: sha256 and byte count.
verify_role() {
  local dir="$1"
  python3 - "$dir" <<'PY'
import hashlib, json, os, sys
root = sys.argv[1]
manifest = json.load(open(os.path.join(root, "MANIFEST.json")))
seen = set()
for entry in manifest["files"]:
    path = os.path.join(root, entry["path"])
    data = open(path, "rb").read()
    if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise SystemExit(f"manifest mismatch: {entry['path']}")
    seen.add(entry["path"])
for base, _dirs, files in os.walk(root):
    for name in files:
        rel = os.path.relpath(os.path.join(base, name), root)
        if rel not in seen and rel not in {"MANIFEST.json", "APPLICATION-RELEASE.json", "provenance.json"}:
            raise SystemExit(f"file outside the manifest: {rel}")
print(f"verified {len(seen)} files in {root}")
PY
}

switch_pointer() {
  # switch_pointer <link> <target>: the same atomic replace the runbook uses.
  ln -sfn "$2" "$1.new" && mv -T "$1.new" "$1"
}

if [[ "$WEB_ONLY" != "--web-only" ]]; then
  API_DST="/opt/radar-v2-api/releases/$REL"
  [[ -e "$API_DST" ]] && die "api release already installed: $API_DST"
  mkdir -m 0750 "$API_DST.new"
  tar -xzf stage/radar-v2-application-release/radar-v2-api.tar.gz --strip-components=1 -C "$API_DST.new"
  cp stage/radar-v2-application-release/provenance.json "$API_DST.new/provenance.json"
  cp APPLICATION-RELEASE.json "$API_DST.new/APPLICATION-RELEASE.json"
  verify_role "$API_DST.new"
  chown -R root:radar-v2-api "$API_DST.new"
  find "$API_DST.new" -type d -exec chmod 0550 {} +
  find "$API_DST.new" -type f -exec chmod 0440 {} +
  chown radar-v2-api:radar-v2-api "$API_DST.new/APPLICATION-RELEASE.json"
  chmod 0400 "$API_DST.new/APPLICATION-RELEASE.json"
  mv -T "$API_DST.new" "$API_DST"
  prev_api="$(readlink /opt/radar-v2-api/current)"
  ln -sfn "$prev_api" "/opt/radar-v2-api/current.before-$REL"
  switch_pointer /opt/radar-v2-api/current "releases/$REL"
  systemctl restart radar-v2-api.service
  # `Type=simple` объявляет службу активной сразу после форка, до bind, поэтому
  # ждём ответа, а не состояния юнита. `|| true` обязательно: под `set -e`
  # ненулевой код curl убивал скрипт прямо здесь — после переключения указателя
  # и до единственного места, где написан откат.
  health=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 2
    health="$(curl -s --max-time 5 http://127.0.0.1:8765/api/health || true)"
    [[ "$health" == *"\"applicationReleaseId\":\"$REL\""* ]] && break
  done
  if [[ "$health" != *"\"applicationReleaseId\":\"$REL\""* ]]; then
    switch_pointer /opt/radar-v2-api/current "$prev_api"
    systemctl restart radar-v2-api.service
    die "health does not name $REL after 20 s: ${health:-нет ответа}; pointer restored to $prev_api"
  fi
  say "api: $health"
  say "api rollback: ln -sfn $prev_api /opt/radar-v2-api/current.new && mv -T /opt/radar-v2-api/current.new /opt/radar-v2-api/current && systemctl restart radar-v2-api.service"
fi

WEB_DST="/srv/radar-v2.aipractice.space/releases/$REL"
[[ -e "$WEB_DST" ]] && die "web release already installed: $WEB_DST"
mkdir -m 0755 "$WEB_DST.new"
tar -xzf stage/radar-v2-application-release/radar-v2-web.tar.gz --strip-components=1 -C "$WEB_DST.new"
cp stage/radar-v2-application-release/provenance.json "$WEB_DST.new/provenance.json"
cp APPLICATION-RELEASE.json "$WEB_DST.new/APPLICATION-RELEASE.json"
verify_role "$WEB_DST.new"
chown -R root:root "$WEB_DST.new"
find "$WEB_DST.new" -type d -exec chmod 0555 {} +
find "$WEB_DST.new" -type f -exec chmod 0444 {} +
mv -T "$WEB_DST.new" "$WEB_DST"
prev_web="$(readlink /srv/radar-v2.aipractice.space/current)"
ln -sfn "$prev_web" "/srv/radar-v2.aipractice.space/current.before-$REL"
switch_pointer /srv/radar-v2.aipractice.space/current "releases/$REL"
say "web: current -> $(readlink /srv/radar-v2.aipractice.space/current)"
# То, что видит вернувшийся читатель: индекс называет собранные токены, и оба
# замороженных ассета по ним отвечают. Проверка стоит здесь, а не на стороне
# оператора, потому что откат тут — одна строка и `prev_web` ещё в руках.
served=""
for _ in 1 2 3; do
  served="$(curl -sS --max-time 15 https://radar.agpm.space/ || true)"
  [[ "$served" == *"/assets/app.mjs?v=$APP_TOKEN"* ]] && break
  sleep 2
done
web_fault=""
[[ "$served" == *"/assets/app.mjs?v=$APP_TOKEN"* ]] || web_fault="index.html не называет app.mjs?v=$APP_TOKEN"
[[ "$served" == *"/assets/styles.css?v=$CSS_TOKEN"* ]] || web_fault="${web_fault:-index.html не называет styles.css?v=$CSS_TOKEN}"
for asset in "app.mjs?v=$APP_TOKEN" "styles.css?v=$CSS_TOKEN"; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://radar.agpm.space/assets/$asset" || true)"
  [[ "$code" == "200" ]] || web_fault="${web_fault:-/assets/$asset отвечает ${code:-нет ответа}}"
done
if [[ -n "$web_fault" ]]; then
  switch_pointer /srv/radar-v2.aipractice.space/current "$prev_web"
  die "$web_fault; web pointer restored to $prev_web"
fi
say "web: reader gets app.mjs?v=$APP_TOKEN and styles.css?v=$CSS_TOKEN"
say "web rollback: ln -sfn $prev_web /srv/radar-v2.aipractice.space/current.new && mv -T /srv/radar-v2.aipractice.space/current.new /srv/radar-v2.aipractice.space/current"
rm -rf stage
REMOTE

say "public health: $(curl -s --max-time 15 https://radar.agpm.space/api/health || true)"
say "done: $rel"
