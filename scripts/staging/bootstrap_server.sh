#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "==> Installing base packages"
sudo apt-get update
sudo apt-get install -y ca-certificates curl

echo "==> Installing Docker"
sudo install -m 0755 -d /etc/apt/keyrings

if [ ! -f /etc/apt/keyrings/docker.asc ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo tee /etc/apt/keyrings/docker.asc >/dev/null
fi

if [ ! -f /etc/apt/sources.list.d/docker.list ]; then
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
fi

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

echo "==> Adding current user to docker group"
sudo usermod -aG docker "$USER" || true

echo "==> Installing uv"
curl -LsSf https://astral.sh/uv/install.sh | sh

if ! grep -qxF 'source $HOME/.local/bin/env' ~/.bashrc 2>/dev/null; then
  echo 'source $HOME/.local/bin/env' >> ~/.bashrc
fi

echo "==> Installing systemd service"
sed -e "s|__USER__|$USER|g" -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
  "$REPO_ROOT/infrastructure/systemd/hybrid_inference.staging.service" \
  | sudo tee /etc/systemd/system/hybrid_inference.staging.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable hybrid_inference.staging

echo "==> Bootstrap complete"
echo "Reconnect to SSH or run: newgrp docker, then run start_staging.sh"
