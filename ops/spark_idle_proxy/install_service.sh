#!/usr/bin/env bash
# Install spark idle proxy as a systemd service.
#
# Usage:
#   sudo ./ops/spark_idle_proxy/install_service.sh
#   sudo ./ops/spark_idle_proxy/install_service.sh --uninstall

set -euo pipefail

REPO_DIR="${REPO_DIR:-/srv/hybridInference}"
SERVICE_NAME="spark_idle_proxy"
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
  echo "       Run this script from the repo root or set REPO_DIR." >&2
  exit 1
fi

echo "Installing ${SERVICE_NAME} …"
cp "$SERVICE_SRC" "$SERVICE_DST"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo "Checking status …"
sleep 2
systemctl status "${SERVICE_NAME}" --no-pager

echo ""
echo "Logs: journalctl -u ${SERVICE_NAME} -f"
