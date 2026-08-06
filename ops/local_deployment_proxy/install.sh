#!/usr/bin/env bash
# ops/local_deployment_proxy/install.sh — install the local_deployment_proxy and
# its reverse tunnel(s) as systemd services on this GPU box.
#
# Installs two units from deploy/systemd/:
#   • local_deployment_proxy.service        — the local listener (LISTEN_PORT)
#   • local_deployment_tunnel@<host>.service — autossh reverse tunnel, one
#                                              instance per router host
# autossh keeps each tunnel alive across link drops; Restart=always plus the
# units' WantedBy=multi-user.target bring everything back after a reboot.
#
# Usage (run on the GPU box, as root):
#   sudo ./local_deployment_proxy/install.sh
# To remove everything, use the companion uninstall.sh.
#
# The units are rendered from deploy/systemd/ with the __REPO_ROOT__ placeholder
# replaced by this checkout's path, so the repo can live anywhere (not just
# /srv/hybridInference).
#
# Override the router hosts and tunnel ports before calling. Each SSH_HOST entry
# is an ssh destination and may include a user (user@host); the tunnel connects
# as that user — defaulting to root, the unit's service user — using root's key.
# Replace 'user' with the router account that authorizes this box's root key:
#   sudo SSH_HOST='user@internal.freeinference.org|user@spark2' REMOTE_PORT=8001 \
#        ./local_deployment_proxy/install.sh
# SSH_HOST is the whole list, not an addition to it: a router an earlier run
# enabled and this one leaves out is stopped and disabled.
#
# On a GPU box with no repo .env, pass the key the gateway signs its requests
# with, or the proxy falls back to the default hardcoded in
# local_deployment_proxy.py and 401s every request once that key is rotated:
#   sudo LOCAL_API_KEY='…' ./local_deployment_proxy/install.sh
# A later run that does not pass it keeps the key already installed; to take the
# key away, pass it empty (sudo LOCAL_API_KEY= ./local_deployment_proxy/install.sh)
# or run uninstall.sh.
#
# Re-running is safe (idempotent): it re-renders the units, reloads systemd,
# re-enables the services, and leaves any installed LOCAL_API_KEY as it is.

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
SSH_HOST="${SSH_HOST:-internal.freeinference.org|spark2}"  # pipe-separated
LISTEN_PORT="${LISTEN_PORT:-8001}"
REMOTE_PORT="${REMOTE_PORT:-8001}"
REMOTE_BIND="${REMOTE_BIND:-0.0.0.0}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SYSTEMD_SRC="${REPO_ROOT}/deploy/systemd"
SYSTEMD_DST="/etc/systemd/system"
PROXY_UNIT="local_deployment_proxy.service"
TUNNEL_UNIT="local_deployment_tunnel@.service"

# shellcheck source=../lib/systemd_local_api_key.sh
source "${REPO_ROOT}/ops/lib/systemd_local_api_key.sh"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  exit 1
fi

# ── autossh dependency ────────────────────────────────────────────────────────
if ! command -v autossh >/dev/null 2>&1; then
  echo "Installing autossh …"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y autossh
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y autossh
  elif command -v yum >/dev/null 2>&1; then
    yum install -y autossh
  else
    echo "ERROR: autossh missing and no known package manager found." >&2
    echo "       Install autossh manually, then re-run." >&2
    exit 1
  fi
fi

# ── Install unit files ────────────────────────────────────────────────────────
# Render a checked-in unit into ${SYSTEMD_DST}, substituting the __REPO_ROOT__
# placeholder with this checkout's path so the installed unit points at wherever
# the repo actually lives. Units with no placeholder are copied unchanged.
render_unit() {
  local name="$1" content tmp
  if [[ ! -f "${SYSTEMD_SRC}/${name}" ]]; then
    echo "ERROR: unit not found: ${SYSTEMD_SRC}/${name}" >&2
    echo "       Run from the repo, or check REPO_ROOT=${REPO_ROOT}." >&2
    exit 1
  fi
  content="$(cat "${SYSTEMD_SRC}/${name}")"
  content="${content//__REPO_ROOT__/$REPO_ROOT}"
  tmp="$(mktemp)"
  printf '%s\n' "$content" >"$tmp"
  install -m 0644 "$tmp" "${SYSTEMD_DST}/${name}"
  rm -f "$tmp"
}

echo "Installing systemd units into ${SYSTEMD_DST} (REPO_ROOT=${REPO_ROOT}) …"
render_unit "$PROXY_UNIT"
render_unit "$TUNNEL_UNIT"

# Written on every run, including the all-defaults one. Writing it only when a value
# differs from the unit's built-in defaults (8001 / 8001 / 0.0.0.0) — which is what
# this did first — leaves the file behind when a later run restores those defaults:
# the block is skipped, the stale drop-in still outranks the unit, and the tunnel
# goes on forwarding the retired port while the gateway has moved back to 8001.
# Silent, and invisible in the installer's output.
DROPIN_DIR="${SYSTEMD_DST}/${TUNNEL_UNIT}.d"
echo "Writing tunnel override (LISTEN_PORT=${LISTEN_PORT} REMOTE_PORT=${REMOTE_PORT} REMOTE_BIND=${REMOTE_BIND}) …"
mkdir -p "$DROPIN_DIR"
cat > "${DROPIN_DIR}/override.conf" <<EOF
[Service]
Environment=LISTEN_PORT=${LISTEN_PORT}
Environment=REMOTE_PORT=${REMOTE_PORT}
Environment=REMOTE_BIND=${REMOTE_BIND}
EOF

# One tunnel instance per '|'-separated SSH destination, cleaned once here so that
# what gets enabled below and what the retirement pass treats as wanted cannot drift
# apart.
declare -a HOSTS=()
IFS='|' read -ra _hosts <<< "$SSH_HOST"
for host in "${_hosts[@]}"; do
  host="${host// /}"
  [[ -z "$host" ]] && continue
  HOSTS+=("$host")
done

if [[ "${#HOSTS[@]}" -eq 0 ]]; then
  # Not merely useless: with the retirement pass below, an empty list would read as
  # "retire every tunnel this box has" and take the node off every gateway.
  echo "ERROR: SSH_HOST is empty, so no gateway could reach this proxy." >&2
  exit 1
fi

# Every tunnel instance systemd knows about — running or merely enabled. The
# discovery uninstall.sh already does, for the same reason: what SSH_HOST names now
# says nothing about what an earlier run left behind. grep -oE is column-agnostic
# (list-units may carry a leading "●" glyph that shifts columns) and the
# @-with-an-instance pattern excludes the bare template unit.
_installed_instances() {
  {
    systemctl list-units --all --type=service --no-legend --no-pager \
      'local_deployment_tunnel@*.service' 2>/dev/null
    systemctl list-unit-files --no-legend --no-pager \
      'local_deployment_tunnel@*.service' 2>/dev/null
  } | grep -oE 'local_deployment_tunnel@[^[:space:]]+\.service' \
    | grep -v '@\.service$' | sort -u
}

# The other half of the tunnel unit's BindsTo=. BindsTo= stops a tunnel whose proxy
# died, so this box cannot advertise a port it is unable to serve — but systemd
# propagates stops and never starts, and the proxy carries Restart=. So a proxy
# crash stops the tunnels, systemd brings the proxy straight back, and the tunnels
# stay down: a healthy proxy behind a dead route, which is how the DGX Spark lost
# diffusiongemma on 2026-08-06. Upholds= is the start-propagating direction, and it
# belongs in a drop-in because the instance names are per-host and the template
# cannot know them.
UPHOLDS_CONF="${SYSTEMD_DST}/${PROXY_UNIT}.d/upholds-tunnels.conf"
echo "Writing ${UPHOLDS_CONF##*/} …"
mkdir -p "${SYSTEMD_DST}/${PROXY_UNIT}.d"
{
  echo "[Unit]"
  # An empty assignment first, so a host dropped from SSH_HOST since the last run is
  # actually gone rather than merged with what this run writes.
  echo "Upholds="
  for host in "${HOSTS[@]}"; do
    echo "Upholds=local_deployment_tunnel@${host}.service"
  done
} > "$UPHOLDS_CONF"

# The blocks above configure the *tunnel* unit. The proxy process needs one env
# var of its own: LOCAL_API_KEY, which it checks every inbound request against and
# otherwise defaults to a value hardcoded in local_deployment_proxy.py — so a
# rotated key turns into a silent 100% 401 rate. See
# ops/lib/systemd_local_api_key.sh for why this is a drop-in and why it
# deliberately loses to the unit's EnvironmentFile=.
#
# The unquoted ${VAR+"$VAR"} passes a third argument only when LOCAL_API_KEY is
# actually set — even if it is set to nothing — and no third argument at all when
# it is unset. "${LOCAL_API_KEY:-}" would hand over an empty string either way,
# and an empty key means "remove the drop-in", so every key-less re-run of this
# installer (a new tunnel host, a different port) would delete the key and the
# restart below would take it away from the running proxy too.
write_local_api_key_dropin "$SYSTEMD_DST" "$PROXY_UNIT" ${LOCAL_API_KEY+"$LOCAL_API_KEY"}

systemctl daemon-reload

# ── Enable + start ────────────────────────────────────────────────────────────
# restart, not `enable --now`: start is a no-op on an already-active unit, so on a
# re-run — the rotation path in the usage notes above — the new key would land in
# /etc/systemd/system while the running proxy kept authenticating with the old
# one. The unit files are re-rendered on every run too, so a bounce is wanted
# regardless of whether the key moved.
echo "Enabling ${PROXY_UNIT} …"
systemctl enable "${PROXY_UNIT}"
systemctl restart "${PROXY_UNIT}"

# Instances an earlier run enabled that this SSH_HOST no longer names. Resetting
# Upholds= above only drops the proxy's dependency on them — each one keeps running
# under Restart=always and keeps its multi-user.target symlink, so a router taken out
# of the list would go on being advertised this box, across reboots, with nothing in
# the installer's output to say so.
while IFS= read -r unit; do
  [[ -z "$unit" ]] && continue
  wanted=0
  for host in "${HOSTS[@]}"; do
    if [[ "$unit" == "local_deployment_tunnel@${host}.service" ]]; then
      wanted=1
      break
    fi
  done
  if [[ "$wanted" -eq 0 ]]; then
    echo "Retiring ${unit} — no longer named by SSH_HOST …"
    systemctl disable --now "$unit" 2>/dev/null || true
    systemctl reset-failed "$unit" 2>/dev/null || true
  fi
done < <(_installed_instances)

for host in "${HOSTS[@]}"; do
  echo "Enabling local_deployment_tunnel@${host} …"
  # enable + restart, for the same reason the proxy above gets it: `--now` starts a
  # stopped unit but is a no-op on a running one, so a re-run that changed the
  # tunnel drop-in (a moved port, a new REMOTE_BIND) would leave the new value on
  # disk and the old one live in the process.
  systemctl enable "local_deployment_tunnel@${host}"
  systemctl restart "local_deployment_tunnel@${host}"
done

echo
echo "Done. Status:"
systemctl --no-pager --no-legend status "${PROXY_UNIT}" 2>/dev/null | head -3 || true
for host in "${HOSTS[@]}"; do
  systemctl --no-pager --no-legend status "local_deployment_tunnel@${host}" 2>/dev/null | head -3 || true
done
echo
echo "Logs:  journalctl -u ${PROXY_UNIT} -f"
echo "       journalctl -u 'local_deployment_tunnel@*' -f"
echo
echo "NOTE: each tunnel connects to its router as the user in the SSH_HOST entry"
echo "      (the part before '@'; defaults to root) using this box's root SSH key"
echo "      in /root/.ssh — the unit runs as root. That key must be authorized in"
echo "      the target account's ~/.ssh/authorized_keys on the router, and the"
echo "      router needs 'GatewayPorts clientspecified' (or yes) in sshd_config"
echo "      for the ${REMOTE_BIND} bind to accept Docker traffic."
