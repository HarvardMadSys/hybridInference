#!/usr/bin/env bash
# Install local deployment proxy as a systemd service.
#
# Usage:
#   sudo ./ops/local_deployment_proxy/install_service.sh          # install & start
#   sudo ./ops/local_deployment_proxy/install_service.sh --uninstall  # remove service
#
# Environment overrides:
#   REPO_DIR=/path/to/hybridInference   # defaults to this checkout's location
#
# (For the proxy + reverse tunnel together, prefer install.sh.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
SERVICE_NAME="local_deployment_proxy"
SERVICE_SRC="${REPO_DIR}/deploy/systemd/${SERVICE_NAME}.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "${1:-}" == "--uninstall" ]]; then
  echo "Stopping and disabling ${SERVICE_NAME} …"
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "$SERVICE_DST"
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
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo "Checking status …"
sleep 2
systemctl status "${SERVICE_NAME}" --no-pager

echo ""
echo "Logs: journalctl -u ${SERVICE_NAME} -f"
