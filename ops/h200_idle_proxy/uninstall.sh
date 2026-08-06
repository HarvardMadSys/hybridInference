#!/usr/bin/env bash
# ops/h200_idle_proxy/uninstall.sh — remove h200_idle_proxy and its tunnels.
#
# Usage:
#   sudo ./ops/h200_idle_proxy/uninstall.sh
#   sudo SSH_HOST='spark2|jason@internal.freeinference.org' \
#        ./ops/h200_idle_proxy/uninstall.sh
#
# REPLICA=b removes the second TP=2 replica's units instead of the first's,
# leaving replica A serving:
#   sudo REPLICA=b ./ops/h200_idle_proxy/uninstall.sh

set -euo pipefail

SSH_HOST="${SSH_HOST:-}"

SYSTEMD_DST="/etc/systemd/system"

# Mirror install.sh: replica B has its own unit names so the two replicas on one
# box are removable independently.
REPLICA="${REPLICA:-a}"
case "$REPLICA" in
  a) PROXY_UNIT="h200_idle_proxy.service"; TUNNEL_BASE="h200_idle_tunnel" ;;
  b) PROXY_UNIT="h200_idle_proxy_b.service"; TUNNEL_BASE="h200_idle_tunnel_b" ;;
  *) echo "ERROR: REPLICA must be 'a' or 'b' (got '${REPLICA}')." >&2; exit 1 ;;
esac
TUNNEL_TMPL="${TUNNEL_BASE}@.service"
DROPIN_DIR="${SYSTEMD_DST}/${TUNNEL_TMPL}.d"
PROXY_DROPIN_DIR="${SYSTEMD_DST}/${PROXY_UNIT}.d"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  exit 1
fi

declare -a INSTANCES=()
if [[ -n "$SSH_HOST" ]]; then
  IFS='|' read -ra HOSTS <<< "$SSH_HOST"
  for host in "${HOSTS[@]}"; do
    host="${host// /}"
    [[ -z "$host" ]] && continue
    INSTANCES+=("${TUNNEL_BASE}@${host}.service")
  done
else
  while IFS= read -r unit; do
    [[ -z "$unit" ]] && continue
    INSTANCES+=("$unit")
  done < <(
    {
      systemctl list-units --all --type=service --no-legend --no-pager \
        "${TUNNEL_BASE}@*.service" 2>/dev/null
      systemctl list-unit-files --no-legend --no-pager \
        "${TUNNEL_BASE}@*.service" 2>/dev/null
    } | grep -oE 'h200_idle_tunnel@[^[:space:]]+\.service' \
      | grep -v '@\.service$' | sort -u
  )
fi

if [[ "${#INSTANCES[@]}" -eq 0 ]]; then
  echo "No tunnel instances found to remove."
else
  for unit in "${INSTANCES[@]}"; do
    echo "Stopping and disabling ${unit} …"
    systemctl disable --now "$unit" 2>/dev/null || true
  done
fi

echo "Stopping and disabling ${PROXY_UNIT} …"
systemctl disable --now "${PROXY_UNIT}" 2>/dev/null || true

echo "Removing unit files from ${SYSTEMD_DST} …"
rm -f "${SYSTEMD_DST}/${PROXY_UNIT}"
rm -f "${SYSTEMD_DST}/${TUNNEL_TMPL}"
rm -rf "$DROPIN_DIR" "$PROXY_DROPIN_DIR"

systemctl daemon-reload
systemctl reset-failed "${PROXY_UNIT}" 2>/dev/null || true
for unit in "${INSTANCES[@]}"; do
  systemctl reset-failed "$unit" 2>/dev/null || true
done

echo
echo "Done. The h200 idle proxy and its reverse tunnel(s) have been removed."
