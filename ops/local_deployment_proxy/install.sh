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
#
# Override the router hosts and tunnel ports before calling:
#   sudo SSH_HOST='internal.freeinference.org|spark2' REMOTE_PORT=8001 \
#        ./local_deployment_proxy/install.sh
#
# Re-running is safe (idempotent): it re-copies the units, reloads systemd, and
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
echo "Installing systemd units into ${SYSTEMD_DST} …"
install -m 0644 "${SYSTEMD_SRC}/${PROXY_UNIT}" "${SYSTEMD_DST}/${PROXY_UNIT}"
install -m 0644 "${SYSTEMD_SRC}/${TUNNEL_UNIT}" "${SYSTEMD_DST}/${TUNNEL_UNIT}"

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
echo "NOTE: root on this box needs an SSH key authorized on each router host,"
echo "      and the routers need 'GatewayPorts clientspecified' in sshd_config"
echo "      for the ${REMOTE_BIND} bind to accept Docker traffic."
