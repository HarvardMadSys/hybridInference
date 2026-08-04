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
#
# On a GPU box with no repo .env, pass the key the gateway signs its requests
# with, or the proxy falls back to the default hardcoded in
# local_deployment_proxy.py and 401s every request once that key is rotated:
#   sudo LOCAL_API_KEY='…' ./local_deployment_proxy/install.sh
#
# Re-running is safe (idempotent): it re-renders the units, reloads systemd, and
# re-enables the services.

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

# Apply port/bind overrides via a drop-in only when they differ from the unit's
# built-in defaults (8001 / 8001 / 0.0.0.0), so the common case stays untouched.
if [[ "$LISTEN_PORT" != "8001" || "$REMOTE_PORT" != "8001" || "$REMOTE_BIND" != "0.0.0.0" ]]; then
  DROPIN_DIR="${SYSTEMD_DST}/${TUNNEL_UNIT}.d"
  echo "Writing tunnel override (LISTEN_PORT=${LISTEN_PORT} REMOTE_PORT=${REMOTE_PORT} REMOTE_BIND=${REMOTE_BIND}) …"
  mkdir -p "$DROPIN_DIR"
  cat > "${DROPIN_DIR}/override.conf" <<EOF
[Service]
Environment=LISTEN_PORT=${LISTEN_PORT}
Environment=REMOTE_PORT=${REMOTE_PORT}
Environment=REMOTE_BIND=${REMOTE_BIND}
EOF
fi

# The block above configures the *tunnel* unit. The proxy process needs one env
# var of its own: LOCAL_API_KEY, which it checks every inbound request against
# and otherwise defaults to a value hardcoded in local_deployment_proxy.py — so a
# rotated key turns into a silent 100% 401 rate. The unit already reads
# ${REPO_ROOT}/.env, which covers a box that also hosts the gateway; a box that
# runs only this proxy has no .env, and this is where the key reaches it.
#
# Written as Environment=, which systemd deliberately ranks *below* an
# EnvironmentFile= (systemd.exec(5): "settings from these files override settings
# made with Environment="). So .env stays the single source of truth wherever it
# exists, and this acts as the fallback for the boxes without one — a rotation
# in .env never has to be chased into /etc/systemd/system.
PROXY_DROPIN="${SYSTEMD_DST}/${PROXY_UNIT}.d"
API_KEY_DROPIN="${PROXY_DROPIN}/local-api-key.conf"
if [[ -n "${LOCAL_API_KEY:-}" ]]; then
  # systemd unquotes the value per systemd.syntax(7); a literal double quote or
  # backslash would survive into the key, so refuse rather than mis-set it.
  if [[ "$LOCAL_API_KEY" == *[\"\\]* ]]; then
    echo "ERROR: LOCAL_API_KEY contains a double quote or backslash, which systemd" >&2
    echo "       would not pass through verbatim. Use a key without them." >&2
    exit 1
  fi
  echo "Writing proxy LOCAL_API_KEY drop-in …"
  mkdir -p "$PROXY_DROPIN"
  cat > "$API_KEY_DROPIN" <<EOF
[Service]
Environment="LOCAL_API_KEY=${LOCAL_API_KEY}"
EOF
  chmod 0600 "$API_KEY_DROPIN"
elif [[ -f "$API_KEY_DROPIN" ]]; then
  # Declarative: omitting LOCAL_API_KEY removes a key left by an earlier run,
  # so a stale one cannot outlive the rotation it was replaced by.
  echo "Removing stale proxy LOCAL_API_KEY drop-in …"
  rm -f "$API_KEY_DROPIN"
fi

systemctl daemon-reload

# ── Enable + start ────────────────────────────────────────────────────────────
echo "Enabling ${PROXY_UNIT} …"
systemctl enable --now "${PROXY_UNIT}"

IFS='|' read -ra HOSTS <<< "$SSH_HOST"
for host in "${HOSTS[@]}"; do
  host="${host// /}"
  [[ -z "$host" ]] && continue
  echo "Enabling local_deployment_tunnel@${host} …"
  systemctl enable --now "local_deployment_tunnel@${host}"
done

echo
echo "Done. Status:"
systemctl --no-pager --no-legend status "${PROXY_UNIT}" 2>/dev/null | head -3 || true
for host in "${HOSTS[@]}"; do
  host="${host// /}"
  [[ -z "$host" ]] && continue
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
