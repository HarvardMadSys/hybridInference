#!/usr/bin/env bash
# ops/h200_idle_proxy/install.sh — install the h200_idle_proxy and its reverse
# tunnels as systemd services on this H200 GPU box.
#
# Installs two units from deploy/systemd/:
#   • h200_idle_proxy.service            — local listener (port 8003)
#   • h200_idle_tunnel@<host>.service    — autossh reverse tunnel per router
#
# Defaults open tunnels to staging (spark2) and production
# (internal.freeinference.org) so both gateways can reach DeepSeek-V4-Flash.
#
# Usage (run on the H200 box, as root):
#   sudo ./ops/h200_idle_proxy/install.sh
#
# Override hosts/ports if needed:
#   sudo SSH_HOST='juncheng@spark2|jason@internal.freeinference.org' \
#        REMOTE_PORT=8003 ./ops/h200_idle_proxy/install.sh
#
# Remove with uninstall.sh.

set -euo pipefail

SSH_HOST="${SSH_HOST:-spark2|jason@internal.freeinference.org}"
LISTEN_PORT="${LISTEN_PORT:-8003}"
REMOTE_PORT="${REMOTE_PORT:-8003}"
REMOTE_BIND="${REMOTE_BIND:-0.0.0.0}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SYSTEMD_SRC="${REPO_ROOT}/deploy/systemd"
SYSTEMD_DST="/etc/systemd/system"
PROXY_UNIT="h200_idle_proxy.service"
TUNNEL_UNIT="h200_idle_tunnel@.service"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  exit 1
fi

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
    exit 1
  fi
fi

render_unit() {
  local name="$1" content tmp
  if [[ ! -f "${SYSTEMD_SRC}/${name}" ]]; then
    echo "ERROR: unit not found: ${SYSTEMD_SRC}/${name}" >&2
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

if [[ "$LISTEN_PORT" != "8003" || "$REMOTE_PORT" != "8003" || "$REMOTE_BIND" != "0.0.0.0" ]]; then
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

# Proxy env drop-in when LISTEN_PORT differs from the unit default.
if [[ "$LISTEN_PORT" != "8003" ]]; then
  PROXY_DROPIN="${SYSTEMD_DST}/${PROXY_UNIT}.d"
  mkdir -p "$PROXY_DROPIN"
  cat > "${PROXY_DROPIN}/override.conf" <<EOF
[Service]
Environment=LISTEN_PORT=${LISTEN_PORT}
EOF
fi

systemctl daemon-reload

echo "Enabling ${PROXY_UNIT} …"
systemctl enable --now "${PROXY_UNIT}"

IFS='|' read -ra HOSTS <<< "$SSH_HOST"
for host in "${HOSTS[@]}"; do
  host="${host// /}"
  [[ -z "$host" ]] && continue
  echo "Enabling h200_idle_tunnel@${host} …"
  systemctl enable --now "h200_idle_tunnel@${host}"
done

echo
echo "Done. Status:"
systemctl --no-pager --no-legend status "${PROXY_UNIT}" 2>/dev/null | head -3 || true
for host in "${HOSTS[@]}"; do
  host="${host// /}"
  [[ -z "$host" ]] && continue
  systemctl --no-pager --no-legend status "h200_idle_tunnel@${host}" 2>/dev/null | head -3 || true
done
echo
echo "Logs:  journalctl -u ${PROXY_UNIT} -f"
echo "       journalctl -u 'h200_idle_tunnel@*' -f"
echo
echo "NOTE: each tunnel connects as the user in the SSH_HOST entry (default root)"
echo "      using this box's root SSH key. That key must be authorized on the"
echo "      router, and the router needs GatewayPorts clientspecified (or yes)."
echo "      Gateways should set H200_DEPLOYMENT_URL=http://host.docker.internal:${REMOTE_PORT}/v1"
