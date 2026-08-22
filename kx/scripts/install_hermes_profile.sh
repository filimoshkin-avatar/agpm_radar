#!/usr/bin/env bash
# Install the Radar KX extraction profile, its egress proxy and the orchestrator unit.
#
# Runs on Local Ru as root. Idempotent: every step checks before it acts, and nothing
# here overwrites an environment file that already exists - a rerun after an operator
# edited a key must not silently undo the edit.
#
# The runtime is copied from the nrd-hermes runtime rather than built with pip.
# ADR-0005 §9 requires the profile to have its own runtime; copying gives it one with
# dependency versions identical to the profile that is already proven on this host,
# and does it without a package resolution that could drift. Only two things in that
# tree carry an absolute path: pyvenv.cfg and one shebang.
set -euo pipefail

RELEASE="${1:-/opt/radar-kx/current}"
RUNTIME=/usr/local/lib/radar-hermes-runtime
DONOR=/usr/local/lib/nrd-hermes-runtime
PROFILE_HOME=/var/lib/radar-hermes/profiles/extraction
ETC=/etc/radar-kx
HERMES_ENV="$ETC/hermes-extraction.env"
ORCHESTRATOR_ENV="$ETC/orchestrator.env"

say() { printf '[install-hermes] %s\n' "$*"; }
die() { printf '[install-hermes] FAIL: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root"
[[ -d "$RELEASE/deploy/hermes-extraction" ]] || die "no profile assets in $RELEASE"
[[ -d /usr/local/lib/hermes-agent ]] || die "hermes-agent is not installed"
[[ -d "$DONOR/venv" && -d "$DONOR/python" ]] || die "no donor runtime at $DONOR"

say "1/8 users"
for user in radar-hermes radar-kx-egress; do
  if ! getent passwd "$user" >/dev/null; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$user"
    say "    created $user"
  else
    say "    $user exists"
  fi
done

say "2/8 runtime"
if [[ ! -x "$RUNTIME/venv/bin/python-radar-kx" ]]; then
  rm -rf "$RUNTIME"
  cp -a "$DONOR" "$RUNTIME"
  sed -i "s#^home = .*#home = $RUNTIME/python/bin#" "$RUNTIME/venv/pyvenv.cfg"
  rm -f "$RUNTIME/venv/bin/python-nrd" "$RUNTIME/venv/bin/hermes-nrd" "$RUNTIME/venv/bin/python"
  ln -s ../../python/bin/python3.11 "$RUNTIME/venv/bin/python-radar-kx"
  ln -s python-radar-kx "$RUNTIME/venv/bin/python"
  {
    printf '#!%s/venv/bin/python-radar-kx\n' "$RUNTIME"
    tail -n +2 "$DONOR/venv/bin/hermes-nrd"
  } > "$RUNTIME/venv/bin/hermes-radar-kx"
  chmod 0755 "$RUNTIME/venv/bin/hermes-radar-kx"
  say "    copied and relocated"
else
  say "    runtime present"
fi
"$RUNTIME/venv/bin/python-radar-kx" -c 'import gateway.platforms.api_server' \
  || die "the runtime cannot import the Hermes API server"
say "    runtime imports the Hermes API server"

say "3/8 profile home"
install -d -o radar-hermes -g radar-hermes -m 0700 /var/lib/radar-hermes
install -d -o radar-hermes -g radar-hermes -m 0700 /var/lib/radar-hermes/profiles
install -d -o radar-hermes -g radar-hermes -m 0700 "$PROFILE_HOME"
install -d -o radar-hermes -g radar-hermes -m 0700 /var/log/radar-kx-hermes-extraction
# Owned by root and read-only to the profile: a profile that can rewrite its own
# contract can pass its own verifier.
for file in config.yaml run_api_only.py verify_profile.py verification-contract.json; do
  install -o root -g radar-hermes -m 0640 "$RELEASE/deploy/hermes-extraction/$file" "$PROFILE_HOME/$file"
done
say "    installed 4 profile files"

say "4/8 environment"
install -d -o root -g radar_kx -m 0750 "$ETC"
if [[ ! -f "$HERMES_ENV" ]]; then
  api_key="$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-40)"
  umask 077
  {
    echo "# Radar KX extraction profile. Root-owned, 0600 (ADR-0005 §9)."
    echo "API_SERVER_KEY=$api_key"
    echo "GLM_API_KEY=CHANGE_ME"
    echo "MINIMAX_API_KEY=CHANGE_ME"
  } > "$HERMES_ENV"
  chown root:root "$HERMES_ENV"
  chmod 0600 "$HERMES_ENV"
  say "    created $HERMES_ENV with a fresh API_SERVER_KEY; provider keys need filling in"
else
  say "    $HERMES_ENV exists, left alone"
fi
if [[ ! -f "$ORCHESTRATOR_ENV" ]]; then
  umask 077
  {
    echo "# The orchestrator's half of the loopback contract. Root-owned, 0600."
    echo "RADAR_KX_HERMES_URL=http://127.0.0.1:19700/v1"
    echo "RADAR_KX_HERMES_KEY=$(grep '^API_SERVER_KEY=' "$HERMES_ENV" | cut -d= -f2-)"
  } > "$ORCHESTRATOR_ENV"
  chown root:root "$ORCHESTRATOR_ENV"
  chmod 0600 "$ORCHESTRATOR_ENV"
  say "    created $ORCHESTRATOR_ENV"
else
  say "    $ORCHESTRATOR_ENV exists, left alone"
fi

say "5/8 units"
for unit in radar-kx-egress-proxy.service radar-kx-hermes-extraction.service radar-kx-orchestrator@.service; do
  install -o root -g root -m 0644 "$RELEASE/deploy/$unit" "/etc/systemd/system/$unit"
done
install -o root -g root -m 0755 "$RELEASE/deploy/kxorch" /usr/local/sbin/kxorch
systemctl daemon-reload
say "    installed 3 units and /usr/local/sbin/kxorch"

say "6/8 egress proxy"
systemctl enable --now radar-kx-egress-proxy.service
systemctl is-active --quiet radar-kx-egress-proxy.service || die "the egress proxy did not start"
say "    active"

say "7/8 extraction profile"
if grep -q 'CHANGE_ME' "$HERMES_ENV"; then
  say "    provider keys are not set; the profile is installed but not started"
else
  systemctl enable --now radar-kx-hermes-extraction.service
  systemctl is-active --quiet radar-kx-hermes-extraction.service || die "the profile did not start"
  say "    active"
fi

say "8/8 state"
systemctl --no-pager --plain list-units 'radar-kx-*' || true
say "done"
