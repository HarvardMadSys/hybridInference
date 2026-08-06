#!/usr/bin/env bash
# Install the spark idle proxy and its reverse tunnels as systemd services.
#
# Installs from deploy/systemd/:
#   • spark_idle_proxy.service          — local listener (port 8002)
#   • spark_idle_tunnel@<host>.service  — autossh reverse tunnel per gateway host
#
# Usage:
#   sudo ./ops/spark_idle_proxy/install_service.sh
#   sudo ./ops/spark_idle_proxy/install_service.sh --tunnels-only
#   sudo ./ops/spark_idle_proxy/install_service.sh --uninstall
#
# The tunnel is what makes this proxy reachable at all. The gateway runs in a
# container on another host and finds the model at host.docker.internal:8002, so
# something has to bind 8002 over there. Until this script installed a unit for
# it, that something was a bare `ssh -N -R` started by hand out of
# spark_idle_service.sh — unsupervised, and on 2026-08-06 it exited and stayed
# dead. The proxy went on answering 200s locally while every gateway request
# failed to connect, so it read as a dead model server and was not one.
#
# On this box the tunnels must run as `juncheng`: root has no SSH key for the
# gateway host and fails host key verification there, so the tunnels have to run as
# `juncheng`:
#   sudo TUNNEL_USER=juncheng ./ops/spark_idle_proxy/install_service.sh
# A later run that does not pass it keeps the user already installed, the way it
# keeps the API key — reverting to the unit's `root` default would leave autossh
# restarting forever. To hand the tunnels back to root, ask for it: TUNNEL_USER=.
#
# Override hosts/ports if needed (SSH_HOST takes a '|'-separated list):
#   sudo SSH_HOST='jason@internal.freeinference.org' REMOTE_PORT=8002 \
#        TUNNEL_USER=juncheng ./ops/spark_idle_proxy/install_service.sh
#
# --tunnels-only installs and bounces the tunnels and leaves the proxy alone.
# Prefer it on a live box: the proxy stops its vLLM containers on SIGTERM, so an
# ordinary run's `systemctl restart` costs the next request a cold start of up to
# HEALTH_TIMEOUT (900s by default). Adding a gateway host does not need that.
#
# The DGX Spark is reached over an SSH tunnel from the gateway host and normally
# carries no repo .env, so the unit's EnvironmentFile= finds nothing there. Pass
# the key the gateway signs its requests with, or the proxy falls back to the
# default hardcoded in spark_idle_proxy.py and 401s every request once that key is
# rotated:
#   sudo LOCAL_API_KEY='…' ./ops/spark_idle_proxy/install_service.sh
# A later run that does not pass it keeps the key already installed; to take the
# key away, pass it empty (LOCAL_API_KEY=) or use --uninstall.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
SERVICE_NAME="spark_idle_proxy"
SERVICE_UNIT="${SERVICE_NAME}.service"
SERVICE_SRC="${REPO_DIR}/deploy/systemd/${SERVICE_UNIT}"
TUNNEL_BASE="spark_idle_tunnel"
TUNNEL_UNIT="${TUNNEL_BASE}@.service"
TUNNEL_SRC="${REPO_DIR}/deploy/systemd/${TUNNEL_UNIT}"
SYSTEMD_DST="/etc/systemd/system"
SERVICE_DST="${SYSTEMD_DST}/${SERVICE_UNIT}"
TUNNEL_DST="${SYSTEMD_DST}/${TUNNEL_UNIT}"

# Production only. Staging deliberately gets no instance: the staging gateway
# runs on this same box and reaches the proxy's own 0.0.0.0:8002 directly, so a
# tunnel to ourselves would be a second listener competing for the same port.
SSH_HOST="${SSH_HOST:-jason@internal.freeinference.org}"
LISTEN_PORT="${LISTEN_PORT:-8002}"
REMOTE_PORT="${REMOTE_PORT:-8002}"
REMOTE_BIND="${REMOTE_BIND:-0.0.0.0}"
# TUNNEL_USER is deliberately *not* defaulted here: the drop-in writer below has to
# tell "unset" (keep whatever is installed) from "set to nothing" (clear it), the
# same distinction write_local_api_key_dropin makes about the API key and for the
# same reason — reverting it silently is an outage, not a cosmetic regression.

# shellcheck source=../lib/systemd_local_api_key.sh
source "${REPO_DIR}/ops/lib/systemd_local_api_key.sh"

MODE="proxy+tunnels"
case "${1:-}" in
  --uninstall) MODE="uninstall" ;;
  --tunnels-only) MODE="tunnels" ;;
  "") ;;
  *)
    echo "ERROR: unknown argument '${1}' (expected --tunnels-only or --uninstall)." >&2
    exit 1
    ;;
esac

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  exit 1
fi

# One tunnel instance per '|'-separated SSH destination.
declare -a TUNNEL_HOSTS=()
IFS='|' read -ra _hosts <<< "$SSH_HOST"
for host in "${_hosts[@]}"; do
  host="${host// /}"
  [[ -z "$host" ]] && continue
  TUNNEL_HOSTS+=("$host")
done

# Every tunnel instance systemd knows about — running or merely enabled. Both
# callers need it, and for the same reason: what SSH_HOST names now says nothing
# about what an earlier run left behind.
_installed_instances() {
  {
    systemctl list-units --all --type=service --no-legend --no-pager \
      "${TUNNEL_BASE}@*.service" 2>/dev/null
    systemctl list-unit-files --no-legend --no-pager \
      "${TUNNEL_BASE}@*.service" 2>/dev/null
  } | grep -oE "${TUNNEL_BASE}@[^[:space:]]+\.service" \
    | grep -v '@\.service$' | sort -u
}

if [[ "$MODE" == "uninstall" ]]; then
  # Every instance systemd knows about, not just the ones SSH_HOST names: a host
  # dropped from the default since install time would otherwise keep a tunnel
  # advertising this box on a gateway nobody is looking at any more.
  declare -a INSTANCES=()
  while IFS= read -r unit; do
    [[ -z "$unit" ]] && continue
    INSTANCES+=("$unit")
  done < <(_installed_instances)
  for unit in "${INSTANCES[@]}"; do
    echo "Stopping and disabling ${unit} …"
    systemctl disable --now "$unit" 2>/dev/null || true
  done

  echo "Stopping and disabling ${SERVICE_NAME} …"
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "$SERVICE_DST" "$TUNNEL_DST"
  # The drop-in holds the API key. Leaving it on a box the operator believes is
  # clean is how a stale key survives to be picked up by a later reinstall.
  rm -rf "${SYSTEMD_DST}/${SERVICE_UNIT}.d" "${SYSTEMD_DST}/${TUNNEL_UNIT}.d"
  systemctl daemon-reload
  systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true
  for unit in "${INSTANCES[@]}"; do
    systemctl reset-failed "$unit" 2>/dev/null || true
  done
  echo "Done."
  exit 0
fi

# Only the units this mode actually renders: --tunnels-only has no business
# requiring the proxy unit to be present, and demanding it turns a legitimate
# tunnels-only run against a partial checkout into a hard error.
declare -a REQUIRED_SRC=("$TUNNEL_SRC")
[[ "$MODE" != "tunnels" ]] && REQUIRED_SRC+=("$SERVICE_SRC")
for src in "${REQUIRED_SRC[@]}"; do
  if [[ ! -f "$src" ]]; then
    echo "ERROR: unit file not found at ${src}" >&2
    echo "       Set REPO_DIR to the hybridInference checkout." >&2
    exit 1
  fi
done

if [[ "${#TUNNEL_HOSTS[@]}" -eq 0 ]]; then
  echo "ERROR: SSH_HOST is empty, so no gateway could reach this proxy." >&2
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

# Render a unit, substituting the __REPO_ROOT__ placeholder with REPO_DIR so the
# installed unit points at this checkout wherever it lives.
render_unit() {
  local name="$1" content tmp
  content="$(cat "${REPO_DIR}/deploy/systemd/${name}")"
  tmp="$(mktemp)"
  printf '%s\n' "${content//__REPO_ROOT__/$REPO_DIR}" >"$tmp"
  install -m 0644 "$tmp" "${SYSTEMD_DST}/${name}"
  rm -f "$tmp"
}

echo "Installing units into ${SYSTEMD_DST} (REPO_DIR=${REPO_DIR}) …"
if [[ "$MODE" != "tunnels" ]]; then
  render_unit "$SERVICE_UNIT"
fi
render_unit "$TUNNEL_UNIT"

DROPIN_DIR="${SYSTEMD_DST}/${TUNNEL_UNIT}.d"
OVERRIDE_CONF="${DROPIN_DIR}/override.conf"

# Written on every run, including the all-defaults one. Writing it only when a value
# differs from the unit's own default — which is what this did first, and what the
# h200 installer still does — leaves the file behind when a later run restores the
# defaults: the block is skipped, the stale drop-in still outranks the unit, and the
# tunnel goes on forwarding the retired port while the gateway has moved back to
# 8002. Silent, and invisible in the installer's output.
#
# User= inverts that rule rather than following it. An unset TUNNEL_USER keeps
# whatever is installed, because on this box the value is load-bearing: root has no
# SSH key for the gateway host, so a re-run that quietly reverted `juncheng` to the
# unit's `root` default would leave autossh restarting forever. Only an explicitly
# empty TUNNEL_USER= clears it. That is the contract the API-key drop-in already
# uses, for the same "absence is not a request to remove" reason.
if [[ -z "${TUNNEL_USER+set}" && -f "$OVERRIDE_CONF" ]]; then
  installed_user="$(sed -n 's/^User=//p' "$OVERRIDE_CONF" | tail -1)"
  if [[ -n "$installed_user" ]]; then
    TUNNEL_USER="$installed_user"
    echo "Keeping installed tunnel User=${TUNNEL_USER} (pass TUNNEL_USER= to clear it)."
  fi
fi
TUNNEL_USER="${TUNNEL_USER:-}"

echo "Writing tunnel override (LISTEN_PORT=${LISTEN_PORT} REMOTE_PORT=${REMOTE_PORT} REMOTE_BIND=${REMOTE_BIND}${TUNNEL_USER:+ User=$TUNNEL_USER}) …"
mkdir -p "$DROPIN_DIR"
cat > "$OVERRIDE_CONF" <<EOF
[Service]
${TUNNEL_USER:+User=${TUNNEL_USER}}
Environment=LISTEN_PORT=${LISTEN_PORT}
Environment=REMOTE_PORT=${REMOTE_PORT}
Environment=REMOTE_BIND=${REMOTE_BIND}
EOF

# The other half of the tunnel unit's BindsTo=. BindsTo= stops a tunnel whose proxy
# died, which is what keeps this box from advertising a port it cannot serve — but
# systemd propagates stops and never starts, and the proxy carries
# Restart=on-failure. So a proxy crash stops the tunnel, systemd brings the proxy
# straight back, and the tunnel stays down: a healthy proxy behind a dead route,
# which is the 2026-08-06 outage. Upholds= is the start-propagating direction; it
# has to live here because the instance name is per-host and the template cannot
# know it.
#
# Its own file, not override.conf: that one is rewritten from LISTEN_PORT and friends
# and this list is keyed on SSH_HOST.
UPHOLDS_CONF="${SYSTEMD_DST}/${SERVICE_UNIT}.d/upholds-tunnels.conf"
echo "Writing ${UPHOLDS_CONF##*/} (${#TUNNEL_HOSTS[@]} tunnel instance(s)) …"
mkdir -p "${SYSTEMD_DST}/${SERVICE_UNIT}.d"
{
  echo "[Unit]"
  # An empty assignment first, so a host dropped from SSH_HOST since the last run is
  # actually gone rather than merged with what this run writes.
  echo "Upholds="
  for host in "${TUNNEL_HOSTS[@]}"; do
    echo "Upholds=${TUNNEL_BASE}@${host}.service"
  done
} > "$UPHOLDS_CONF"

if [[ "$MODE" != "tunnels" ]]; then
  # The unquoted ${VAR+"$VAR"} passes a third argument only when LOCAL_API_KEY is
  # set (even when set to nothing), and none at all when it is unset. An empty key
  # means "remove the drop-in", so collapsing the two would make the key-less
  # invocation in the usage notes above delete the Spark's key and — via the restart
  # below — 401 every request it then serves.
  write_local_api_key_dropin "$SYSTEMD_DST" "$SERVICE_UNIT" ${LOCAL_API_KEY+"$LOCAL_API_KEY"}
fi

systemctl daemon-reload

if [[ "$MODE" != "tunnels" ]]; then
  # restart, not `enable --now`: start is a no-op on an already-active unit, so a
  # re-run would leave the new key on disk and the old one live in the process.
  echo "Enabling ${SERVICE_NAME} …"
  systemctl enable "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"
fi

# Instances an earlier run enabled that this SSH_HOST no longer names. Resetting
# Upholds= above only drops the proxy's dependency on them — each one keeps running
# under Restart=always and keeps its multi-user.target symlink, so a gateway host
# taken out of the list would go on being advertised this box, across reboots, with
# nothing in the installer's output to say so.
while IFS= read -r unit; do
  [[ -z "$unit" ]] && continue
  wanted=0
  for host in "${TUNNEL_HOSTS[@]}"; do
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

for host in "${TUNNEL_HOSTS[@]}"; do
  echo "Enabling ${TUNNEL_BASE}@${host} …"
  # enable + restart, not `enable --now`: --now is a no-op on an already-active
  # unit, so a re-run that changes the tunnel drop-in (a new TUNNEL_USER, say)
  # would leave the new value on disk and the old one live in the process --
  # exactly the trap the proxy unit above already sidesteps for its API key.
  systemctl enable "${TUNNEL_BASE}@${host}"
  systemctl restart "${TUNNEL_BASE}@${host}"
done

echo
echo "Checking status …"
sleep 2
if [[ "$MODE" != "tunnels" ]]; then
  systemctl --no-pager --no-legend status "${SERVICE_NAME}" 2>/dev/null | head -3 || true
fi
for host in "${TUNNEL_HOSTS[@]}"; do
  systemctl --no-pager --no-legend status "${TUNNEL_BASE}@${host}" 2>/dev/null | head -3 || true
done

echo
echo "Logs: journalctl -u ${SERVICE_NAME} -f"
echo "      journalctl -u '${TUNNEL_BASE}@*' -f"
echo
echo "NOTE: each tunnel connects as the user in the SSH_HOST entry, over the SSH"
echo "      key of the user the unit runs as (root unless TUNNEL_USER is set)."
echo "      That key must be authorized on the gateway host, and the gateway host"
echo "      needs GatewayPorts clientspecified (or yes) for the 0.0.0.0 bind."
echo "      Gateways should set SPARK_DEPLOYMENT_URL=http://host.docker.internal:${REMOTE_PORT}/v1"
