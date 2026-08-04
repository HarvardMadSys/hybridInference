#!/usr/bin/env bash
# ops/local_deployment_proxy/uninstall.sh — remove the local_deployment_proxy and
# its reverse tunnel(s) installed by install.sh from this GPU box.
#
# Reverses install.sh: stops and disables the proxy unit and every tunnel
# instance, then removes the unit files and every drop-in either of them left.
#
# Removes:
#   • local_deployment_proxy.service         — the local listener
#   • local_deployment_proxy.service.d/      — its LOCAL_API_KEY drop-in
#   • local_deployment_tunnel@<host>.service — every autossh tunnel instance
#   • local_deployment_tunnel@.service       — the tunnel template unit
#   • local_deployment_tunnel@.service.d/    — the optional override drop-in
#
# Tunnel instances are discovered automatically from systemd, so you do not need
# to pass the same SSH_HOST you installed with. You can still scope the removal
# to specific hosts if you want:
#   sudo SSH_HOST='internal.freeinference.org|spark2' \
#        ./local_deployment_proxy/uninstall.sh
#
# autossh (the system package install.sh may have installed) is intentionally
# left in place — removing a shared package on uninstall would be surprising.
#
# Re-running is safe (idempotent).

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
SSH_HOST="${SSH_HOST:-}"  # optional, pipe-separated; empty = discover all

SYSTEMD_DST="/etc/systemd/system"
PROXY_UNIT="local_deployment_proxy.service"
TUNNEL_TMPL="local_deployment_tunnel@.service"
DROPIN_DIR="${SYSTEMD_DST}/${TUNNEL_TMPL}.d"
# install.sh writes the proxy's LOCAL_API_KEY here. It is a credential, so an
# uninstall that left it behind would leave the key readable on a box the
# operator believes is clean — and a later reinstall would silently inherit it.
PROXY_DROPIN_DIR="${SYSTEMD_DST}/${PROXY_UNIT}.d"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  exit 1
fi

# ── Determine which tunnel instances to remove ────────────────────────────────
# Either the hosts the caller named, or every instance systemd knows about
# (both live units and enabled-but-stopped unit files).
declare -a INSTANCES=()
if [[ -n "$SSH_HOST" ]]; then
  IFS='|' read -ra HOSTS <<< "$SSH_HOST"
  for host in "${HOSTS[@]}"; do
    host="${host// /}"
    [[ -z "$host" ]] && continue
    INSTANCES+=("local_deployment_tunnel@${host}.service")
  done
else
  while IFS= read -r unit; do
    [[ -z "$unit" ]] && continue
    INSTANCES+=("$unit")
  done < <(
    # Pull the unit token out of both `list-units` (live, may carry a leading
    # "●" status glyph that shifts columns) and `list-unit-files` (enabled but
    # stopped). grep -oE is column-agnostic; the @-with-an-instance pattern
    # excludes the bare template unit.
    {
      systemctl list-units --all --type=service --no-legend --no-pager \
        'local_deployment_tunnel@*.service' 2>/dev/null
      systemctl list-unit-files --no-legend --no-pager \
        'local_deployment_tunnel@*.service' 2>/dev/null
    } | grep -oE 'local_deployment_tunnel@[^[:space:]]+\.service' \
      | grep -v '@\.service$' | sort -u
  )
fi

# ── Stop + disable tunnel instances ───────────────────────────────────────────
if [[ "${#INSTANCES[@]}" -eq 0 ]]; then
  echo "No tunnel instances found to remove."
else
  for unit in "${INSTANCES[@]}"; do
    echo "Stopping and disabling ${unit} …"
    systemctl disable --now "$unit" 2>/dev/null || true
  done
fi

# ── Stop + disable the proxy ──────────────────────────────────────────────────
echo "Stopping and disabling ${PROXY_UNIT} …"
systemctl disable --now "${PROXY_UNIT}" 2>/dev/null || true

# ── Remove unit files + drop-ins ──────────────────────────────────────────────
echo "Removing unit files from ${SYSTEMD_DST} …"
rm -f "${SYSTEMD_DST}/${PROXY_UNIT}"
rm -f "${SYSTEMD_DST}/${TUNNEL_TMPL}"
for dir in "$DROPIN_DIR" "$PROXY_DROPIN_DIR"; do
  if [[ -d "$dir" ]]; then
    echo "Removing drop-in ${dir} …"
    rm -rf "$dir"
  fi
done

systemctl daemon-reload
systemctl reset-failed "${PROXY_UNIT}" 'local_deployment_tunnel@*' 2>/dev/null || true

echo
echo "Done. The proxy and its reverse tunnel(s) have been removed."
echo "NOTE: autossh was left installed; remove it manually if you no longer need it."
