#!/usr/bin/env bash
# Install spark idle proxy as a systemd service.
#
# Usage:
#   sudo ./ops/spark_idle_proxy/install_service.sh
#   sudo ./ops/spark_idle_proxy/install_service.sh --uninstall
#
# The DGX Spark is reached over an SSH tunnel from the gateway host and normally
# carries no repo .env, so the unit's EnvironmentFile= finds nothing there. Pass
# the key the gateway signs its requests with, or the proxy falls back to the
# default hardcoded in spark_idle_proxy.py and 401s every request once that key is
# rotated. Omitting it removes a key an earlier run left:
#   sudo LOCAL_API_KEY='…' ./ops/spark_idle_proxy/install_service.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
SERVICE_NAME="spark_idle_proxy"
SERVICE_UNIT="${SERVICE_NAME}.service"
SERVICE_SRC="${REPO_DIR}/deploy/systemd/${SERVICE_UNIT}"
SYSTEMD_DST="/etc/systemd/system"
SERVICE_DST="${SYSTEMD_DST}/${SERVICE_UNIT}"

# shellcheck source=../lib/systemd_local_api_key.sh
source "${REPO_DIR}/ops/lib/systemd_local_api_key.sh"

if [[ "${1:-}" == "--uninstall" ]]; then
  echo "Stopping and disabling ${SERVICE_NAME} …"
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "$SERVICE_DST"
  # The drop-in holds the API key. Leaving it on a box the operator believes is
  # clean is how a stale key survives to be picked up by a later reinstall.
  rm -rf "${SYSTEMD_DST}/${SERVICE_UNIT}.d"
  systemctl daemon-reload
  echo "Done."
  exit 0
fi

if [[ ! -f "$SERVICE_SRC" ]]; then
  echo "ERROR: Service file not found at ${SERVICE_SRC}" >&2
  echo "       Set REPO_DIR to the hybridInference checkout." >&2
  exit 1
fi

# Render the unit, substituting the __REPO_ROOT__ placeholder with REPO_DIR so
# the installed unit points at this checkout wherever it lives.
echo "Installing ${SERVICE_NAME} (REPO_DIR=${REPO_DIR}) …"
tmp="$(mktemp)"
content="$(cat "$SERVICE_SRC")"
printf '%s\n' "${content//__REPO_ROOT__/$REPO_DIR}" >"$tmp"
install -m 0644 "$tmp" "$SERVICE_DST"
rm -f "$tmp"

write_local_api_key_dropin "$SYSTEMD_DST" "$SERVICE_UNIT" "${LOCAL_API_KEY:-}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo "Checking status …"
sleep 2
systemctl status "${SERVICE_NAME}" --no-pager

echo ""
echo "Logs: journalctl -u ${SERVICE_NAME} -f"
