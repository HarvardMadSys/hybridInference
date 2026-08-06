#!/usr/bin/env bash
# ops/h200_idle_proxy/install.sh — install the h200_idle_proxy and its reverse
# tunnels as systemd services on this H200 GPU box.
#
# Installs two units from deploy/systemd/:
#   • h200_idle_proxy.service            — local listener (port 8003)
#   • h200_idle_tunnel@<host>.service    — autossh reverse tunnel per router
#
# A 4xH200 box runs TWO TP=2 replicas (see REPLICA below). Replica B installs its
# own separately-named units, so it never overwrites replica A:
#   sudo REPLICA=b ./ops/h200_idle_proxy/install.sh
#
# On h200a the tunnels must run as `juncheng` — root there has no SSH key for the
# routers — so pass TUNNEL_USER so a reinstall does not revert that:
#   sudo REPLICA=b TUNNEL_USER=juncheng ./ops/h200_idle_proxy/install.sh
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
# On a box with no repo .env, pass the key the gateway signs its requests with, or
# the proxy falls back to the default hardcoded in local_deployment_proxy.py and
# 401s every request once that key is rotated:
#   sudo LOCAL_API_KEY='…' ./ops/h200_idle_proxy/install.sh
# A later run that does not pass it keeps the key already installed; to take the
# key away, pass it empty (LOCAL_API_KEY=) or run uninstall.sh.
#
# Remove with uninstall.sh.

set -euo pipefail

SSH_HOST="${SSH_HOST:-spark2|jason@internal.freeinference.org}"
REMOTE_BIND="${REMOTE_BIND:-0.0.0.0}"

# REPLICA selects which of this box's DeepSeek replicas to install.
#
#   a (default) — h200_idle_proxy.service, TP=2 on GPUs 2,3, port 8003 (h200b: 8004)
#   b           — h200_idle_proxy_b.service, TP=2 on GPUs 0,1, port 8005
#
# A 4xH200 box runs two TP=2 replicas rather than one TP=4 instance because TP=4
# scales at only ~0.71 efficiency on these cards (measured on h200a: one TP=2
# replica does 4,215 decode tok/s at c=128 vs TP=4's 6,014, so two project to
# ~8,430). The replicas are separate units with separate unit *names* on purpose:
# installing B must never overwrite A. Before this flag existed, a re-run with a
# different LISTEN_PORT rewrote the single h200_idle_proxy.service and its
# drop-ins in place, so there was no way to have two of them on one host.
REPLICA="${REPLICA:-a}"
case "$REPLICA" in
  a)
    PROXY_UNIT="h200_idle_proxy.service"
    TUNNEL_BASE="h200_idle_tunnel"
    DEFAULT_PORT=8003
    ;;
  b)
    PROXY_UNIT="h200_idle_proxy_b.service"
    TUNNEL_BASE="h200_idle_tunnel_b"
    DEFAULT_PORT=8005
    ;;
  *)
    echo "ERROR: REPLICA must be 'a' or 'b' (got '${REPLICA}')." >&2
    exit 1
    ;;
esac
TUNNEL_UNIT="${TUNNEL_BASE}@.service"
LISTEN_PORT="${LISTEN_PORT:-$DEFAULT_PORT}"
REMOTE_PORT="${REMOTE_PORT:-$DEFAULT_PORT}"

# TUNNEL_USER writes a `User=` drop-in for the tunnel unit. The units default to
# root, which is wrong on any box where root has no SSH key or config for the
# routers -- h200a is such a box, and its replica A tunnels already run as
# `juncheng` through a hand-written override. Pass TUNNEL_USER=juncheng so a
# reinstall does not silently revert that and leave the tunnels unable to
# authenticate.
TUNNEL_USER="${TUNNEL_USER:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SYSTEMD_SRC="${REPO_ROOT}/deploy/systemd"
SYSTEMD_DST="/etc/systemd/system"

# shellcheck source=../lib/systemd_local_api_key.sh
source "${REPO_ROOT}/ops/lib/systemd_local_api_key.sh"

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

if [[ "$LISTEN_PORT" != "$DEFAULT_PORT" || "$REMOTE_PORT" != "$DEFAULT_PORT" \
      || "$REMOTE_BIND" != "0.0.0.0" || -n "$TUNNEL_USER" ]]; then
  DROPIN_DIR="${SYSTEMD_DST}/${TUNNEL_UNIT}.d"
  echo "Writing tunnel override (LISTEN_PORT=${LISTEN_PORT} REMOTE_PORT=${REMOTE_PORT} REMOTE_BIND=${REMOTE_BIND}${TUNNEL_USER:+ User=$TUNNEL_USER}) …"
  mkdir -p "$DROPIN_DIR"
  cat > "${DROPIN_DIR}/override.conf" <<EOF
[Service]
${TUNNEL_USER:+User=${TUNNEL_USER}}
Environment=LISTEN_PORT=${LISTEN_PORT}
Environment=REMOTE_PORT=${REMOTE_PORT}
Environment=REMOTE_BIND=${REMOTE_BIND}
EOF
fi

# Proxy env drop-in when LISTEN_PORT differs from this replica's unit default.
if [[ "$LISTEN_PORT" != "$DEFAULT_PORT" ]]; then
  PROXY_DROPIN="${SYSTEMD_DST}/${PROXY_UNIT}.d"
  mkdir -p "$PROXY_DROPIN"
  cat > "${PROXY_DROPIN}/override.conf" <<EOF
[Service]
Environment=LISTEN_PORT=${LISTEN_PORT}
EOF
fi

# Both H200 nodes authenticate against the same LOCAL_API_KEY the gateway signs
# with. The unit reads ${REPO_ROOT}/.env, which covers a box that also hosts the
# gateway; on a box that runs only this proxy the key arrives here instead.
#
# The unquoted ${VAR+"$VAR"} passes a third argument only when LOCAL_API_KEY is
# set (even when set to nothing), and none at all when it is unset — an empty key
# means "remove the drop-in", and a port-only re-run such as the LISTEN_PORT=8004
# one in the usage notes above must not take the key off the node.
write_local_api_key_dropin "$SYSTEMD_DST" "$PROXY_UNIT" ${LOCAL_API_KEY+"$LOCAL_API_KEY"}

systemctl daemon-reload

# restart, not `enable --now`: start is a no-op on an already-active unit, so a
# re-run would leave the new key on disk and the old one live in the process.
echo "Enabling ${PROXY_UNIT} …"
systemctl enable "${PROXY_UNIT}"
systemctl restart "${PROXY_UNIT}"

IFS='|' read -ra HOSTS <<< "$SSH_HOST"
for host in "${HOSTS[@]}"; do
  host="${host// /}"
  [[ -z "$host" ]] && continue
  echo "Enabling ${TUNNEL_BASE}@${host} …"
  # enable + restart, not `enable --now`: --now is a no-op on an already-active
  # unit, so a re-run that changes the tunnel drop-in (a new TUNNEL_USER, say)
  # would leave the new value on disk and the old one live in the process --
  # exactly the trap the proxy unit above already sidesteps for its API key.
  systemctl enable "${TUNNEL_BASE}@${host}"
  systemctl restart "${TUNNEL_BASE}@${host}"
done

echo
echo "Done. Status:"
systemctl --no-pager --no-legend status "${PROXY_UNIT}" 2>/dev/null | head -3 || true
for host in "${HOSTS[@]}"; do
  host="${host// /}"
  [[ -z "$host" ]] && continue
  systemctl --no-pager --no-legend status "${TUNNEL_BASE}@${host}" 2>/dev/null | head -3 || true
done
echo
echo "Logs:  journalctl -u ${PROXY_UNIT} -f"
echo "       journalctl -u '${TUNNEL_BASE}@*' -f"
echo
echo "NOTE: each tunnel connects as the user in the SSH_HOST entry (default root)"
echo "      using this box's root SSH key. That key must be authorized on the"
echo "      router, and the router needs GatewayPorts clientspecified (or yes)."
# The two H200 nodes are distinguished by remote port: h200a keeps the original
# 8003 / H200_DEPLOYMENT_URL pair, h200b uses 8004 / H200B_DEPLOYMENT_URL.
case "$REMOTE_PORT" in
  8004) GATEWAY_VAR=H200B_DEPLOYMENT_URL ;;
  8005) GATEWAY_VAR=H200A2_DEPLOYMENT_URL ;;
  *) GATEWAY_VAR=H200_DEPLOYMENT_URL ;;
esac
echo "      Gateways should set ${GATEWAY_VAR}=http://host.docker.internal:${REMOTE_PORT}/v1"
