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
# routers:
#   sudo REPLICA=b TUNNEL_USER=juncheng ./ops/h200_idle_proxy/install.sh
# A later run that does not pass it keeps the user already installed, the way it
# keeps the API key — reverting to the unit's `root` default would leave autossh
# restarting forever. To hand the tunnels back to root, ask for it: TUNNEL_USER=.
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
# SSH_HOST is the whole list, not an addition to it: a router an earlier run
# enabled and this one leaves out is stopped and disabled. Only this replica's
# tunnels are considered, so installing A never retires B's.
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
# routers -- h200a is such a box, and its tunnels run as `juncheng`.
#
# It is deliberately *not* defaulted here. The drop-in writer below has to tell
# "unset" (keep whatever is installed) from "set to nothing" (clear it), the same
# distinction write_local_api_key_dropin makes about the API key and for the same
# reason: on h200a a re-run that quietly reverted `juncheng` to the unit's `root`
# default would leave autossh unable to authenticate and restarting forever. That
# is an outage, not a cosmetic regression, so absence must not mean removal.

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

DROPIN_DIR="${SYSTEMD_DST}/${TUNNEL_UNIT}.d"
OVERRIDE_CONF="${DROPIN_DIR}/override.conf"

# User= inverts the write-it-always rule below rather than following it. An unset
# TUNNEL_USER keeps whatever is installed, because on h200a the value is
# load-bearing: root there has no SSH key for the routers, so a re-run that
# reverted `juncheng` to the unit's `root` default would leave autossh restarting
# forever. Only an explicitly empty TUNNEL_USER= clears it -- the contract the
# API-key drop-in already uses, for the same "absence is not a request to remove"
# reason.
if [[ -z "${TUNNEL_USER+set}" && -f "$OVERRIDE_CONF" ]]; then
  installed_user="$(sed -n 's/^User=//p' "$OVERRIDE_CONF" | tail -1)"
  if [[ -n "$installed_user" ]]; then
    TUNNEL_USER="$installed_user"
    echo "Keeping installed tunnel User=${TUNNEL_USER} (pass TUNNEL_USER= to clear it)."
  fi
fi
TUNNEL_USER="${TUNNEL_USER:-}"

# Written on every run, including the all-defaults one. Writing it only when a value
# differs from the unit's own default -- which is what this did first -- leaves the
# file behind when a later run restores the defaults: the block is skipped, the stale
# drop-in still outranks the unit, and the tunnel goes on forwarding the retired port
# while the gateway has moved back. Silent, and invisible in the installer's output.
echo "Writing tunnel override (LISTEN_PORT=${LISTEN_PORT} REMOTE_PORT=${REMOTE_PORT} REMOTE_BIND=${REMOTE_BIND}${TUNNEL_USER:+ User=$TUNNEL_USER}) …"
mkdir -p "$DROPIN_DIR"
cat > "$OVERRIDE_CONF" <<EOF
[Service]
${TUNNEL_USER:+User=${TUNNEL_USER}}
Environment=LISTEN_PORT=${LISTEN_PORT}
Environment=REMOTE_PORT=${REMOTE_PORT}
Environment=REMOTE_BIND=${REMOTE_BIND}
EOF

# The proxy's own LISTEN_PORT, written unconditionally for the same reason and
# additionally because the tunnel drop-in above now is. The two have to move
# together: leaving this one conditional means a run that restores the default port
# retargets the tunnel to ${DEFAULT_PORT} while a stale drop-in keeps the proxy
# listening on the retired one -- a mismatch that could not happen while both were
# skipped in lockstep.
PROXY_DROPIN="${SYSTEMD_DST}/${PROXY_UNIT}.d"
mkdir -p "$PROXY_DROPIN"
cat > "${PROXY_DROPIN}/override.conf" <<EOF
[Service]
Environment=LISTEN_PORT=${LISTEN_PORT}
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
  # "retire every tunnel this box has" and take the node off both gateways.
  echo "ERROR: SSH_HOST is empty, so no gateway could reach this proxy." >&2
  exit 1
fi

# Every tunnel instance systemd knows about for *this replica* -- running or merely
# enabled. What SSH_HOST names now says nothing about what an earlier run left
# behind. The '@' in the pattern is what scopes it: replica A's `h200_idle_tunnel@`
# cannot match replica B's `h200_idle_tunnel_b@…`, so installing one never retires
# the other's tunnels.
_installed_instances() {
  {
    systemctl list-units --all --type=service --no-legend --no-pager \
      "${TUNNEL_BASE}@*.service" 2>/dev/null
    systemctl list-unit-files --no-legend --no-pager \
      "${TUNNEL_BASE}@*.service" 2>/dev/null
  } | grep -oE "${TUNNEL_BASE}@[^[:space:]]+\.service" \
    | grep -v '@\.service$' | sort -u
}

# The other half of the tunnel units' BindsTo=. BindsTo= stops a tunnel whose proxy
# died, which is what keeps this node from advertising a port it cannot serve — but
# systemd propagates stops and never starts, and the proxy carries
# Restart=on-failure. So a proxy crash stops the tunnels, systemd brings the proxy
# straight back, and the tunnels stay down: a healthy proxy behind a dead route,
# which is how the DGX Spark lost diffusiongemma on 2026-08-06. Upholds= is the
# start-propagating direction, and it has to live in a drop-in because the instance
# names are per-host and the template cannot know them.
#
# Its own file, not the override.conf above: that one is keyed on the ports, this
# one on SSH_HOST.
UPHOLDS_CONF="${SYSTEMD_DST}/${PROXY_UNIT}.d/upholds-tunnels.conf"
echo "Writing ${UPHOLDS_CONF##*/} …"
mkdir -p "${SYSTEMD_DST}/${PROXY_UNIT}.d"
{
  echo "[Unit]"
  # An empty assignment first, so a host dropped from SSH_HOST since the last run is
  # actually gone rather than merged with what this run writes.
  echo "Upholds="
  for host in "${HOSTS[@]}"; do
    echo "Upholds=${TUNNEL_BASE}@${host}.service"
  done
} > "$UPHOLDS_CONF"

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

# Instances an earlier run enabled that this SSH_HOST no longer names. Resetting
# Upholds= above only drops the proxy's dependency on them -- each one keeps running
# under Restart=always and keeps its multi-user.target symlink, so a router taken out
# of the list would go on being advertised this box, across reboots, with nothing in
# the installer's output to say so.
while IFS= read -r unit; do
  [[ -z "$unit" ]] && continue
  wanted=0
  for host in "${HOSTS[@]}"; do
    if [[ "$unit" == "${TUNNEL_BASE}@${host}.service" ]]; then
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
