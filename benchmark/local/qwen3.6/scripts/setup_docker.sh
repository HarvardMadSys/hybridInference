#!/usr/bin/env bash
# setup_docker.sh — install Docker Engine and NVIDIA Container Toolkit on Ubuntu.
# Run as root (or with sudo). Idempotent: safe to re-run.
#
# Verification step at the end runs:
#   docker run --rm --gpus all nvidia/cuda:12.6.2-base-ubuntu22.04 nvidia-smi
# and exits non-zero if it fails.

set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
  echo "This script must be run with sudo." >&2
  exit 1
fi

# 1. Docker Engine via convenience script
if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker Engine..."
  curl -fsSL https://get.docker.com | sh
fi

# 2. Add the calling user (the SUDO_USER) to the docker group
if [[ -n "${SUDO_USER:-}" ]]; then
  usermod -aG docker "$SUDO_USER" || true
fi

# 3. NVIDIA Container Toolkit (Ubuntu/Debian path)
if ! command -v nvidia-ctk >/dev/null 2>&1; then
  echo "Installing NVIDIA Container Toolkit..."
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update
  apt-get install -y nvidia-container-toolkit
fi

# 4. Configure Docker to use the nvidia runtime
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

# 5. Verify
echo "Verifying GPU is visible inside containers..."
docker run --rm --gpus all nvidia/cuda:12.6.2-base-ubuntu22.04 nvidia-smi
echo "OK"
