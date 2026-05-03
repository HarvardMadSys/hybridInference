#!/usr/bin/env bash
# Install and run a Cloudflare Tunnel (cloudflared) as a systemd service.
#
# The tunnel routes staging.freeinference.org → nginx on localhost,
# with no inbound ports required on the server.
#
# Usage:
#   sudo TUNNEL_TOKEN=<token> ./ops/setup/setup_cloudflared.sh
#
# Or:
#   sudo ./ops/setup/setup_cloudflared.sh --token <token>
#
# Get the token from:
#   Cloudflare Dashboard → Zero Trust → Networks → Tunnels → Create a tunnel
#   → name it "staging" → copy the token shown on the next screen

set -Eeuo pipefail

log() { printf '[setup-cloudflared] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# ── Argument parsing ──────────────────────────────────────────────────────────

TUNNEL_TOKEN="${TUNNEL_TOKEN:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --token) TUNNEL_TOKEN="$2"; shift 2 ;;
        *) die "Unknown argument: $1. Use --token or TUNNEL_TOKEN env var." ;;
    esac
done

[[ -n "$TUNNEL_TOKEN" ]] || die "Provide the tunnel token via --token or TUNNEL_TOKEN env var."
[[ $EUID -eq 0 ]] || die "Must run as root (use sudo)."

# ── 1. Install cloudflared ────────────────────────────────────────────────────

log "Installing cloudflared..."

# Prerequisites — required even on minimal/fresh hosts so the apt key fetch and
# HTTPS apt source below work.
log "Installing prerequisites (curl, gnupg, ca-certificates)..."
apt-get update -q
apt-get install -y curl gnupg ca-certificates

if ! command -v cloudflared &>/dev/null; then
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg

    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(. /etc/os-release && echo "$VERSION_CODENAME") main" \
        | tee /etc/apt/sources.list.d/cloudflared.list > /dev/null

    apt-get update -q
    apt-get install -y cloudflared
else
    log "cloudflared already installed ($(cloudflared --version 2>&1 | head -1))."
fi

# ── 2. Install as systemd service ────────────────────────────────────────────

# `cloudflared service install` fails if the service unit already exists, so
# uninstall first when present to keep the script idempotent on re-runs.
if systemctl list-unit-files 2>/dev/null | grep -q '^cloudflared\.service'; then
    log "Existing cloudflared service detected — uninstalling before reinstall."
    cloudflared service uninstall || true
fi

log "Installing cloudflared service..."
cloudflared service install "$TUNNEL_TOKEN"

# ── 3. Start ──────────────────────────────────────────────────────────────────

log "Starting cloudflared service..."
systemctl enable cloudflared
systemctl start cloudflared

# ── 4. Verify ─────────────────────────────────────────────────────────────────

sleep 3
if systemctl is-active --quiet cloudflared; then
    log "OK — cloudflared is running."
else
    log "cloudflared failed to start. Check: journalctl -u cloudflared -n 50"
    exit 1
fi

log "Tunnel is up. Traffic from Cloudflare will now reach this server."
log "Verify: curl -I https://staging.freeinference.org/"
