#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────
# FreeInference — Claude Code one-click setup script
#
# Usage:
#   # Recommended: download first, inspect, then run
#   curl -fsSL -o setup_claude_code.sh https://doc.freeinference.org/setup_claude_code.sh
#   bash setup_claude_code.sh
#
#   # Or from a cloned repo:
#   bash ops/setup/setup_claude_code.sh
#
# What it does:
#   1. Checks that Claude Code (claude) is installed
#   2. Asks for your FreeInference API key
#   3. Merges the gateway and model settings into ~/.claude/settings.json
#   4. Runs a quick connectivity test against the proxy
# ──────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Configurable defaults ────────────────────────────────────────
FREEINFERENCE_BASE_URL="https://freeinference.org/anthropic"
# Pin explicit public models for predictable quality/availability. Main:
# minimax-m3 (long context, image input). Haiku/background: qwen3.6-35b
# (fast, non-reasoning). Override via env for other accessible models.
FREEINFERENCE_MODEL="${FREEINFERENCE_MODEL:-minimax-m3}"
FREEINFERENCE_HAIKU_MODEL="${FREEINFERENCE_HAIKU_MODEL:-${FREEINFERENCE_SMALL_FAST_MODEL:-qwen3.6-35b}}"
SETTINGS_FILE="${HOME}/.claude/settings.json"
TEST_ENDPOINT="https://freeinference.org/anthropic/v1/messages"

# ── Colors ───────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()  { printf "${CYAN}ℹ${NC}  %s\n" "$*"; }
ok()    { printf "${GREEN}✔${NC}  %s\n" "$*"; }
warn()  { printf "${YELLOW}⚠${NC}  %s\n" "$*"; }
err()   { printf "${RED}✘${NC}  %s\n" "$*" >&2; }

# ── 1. Check Claude Code is installed ────────────────────────────
printf "\n${BOLD}🚀 FreeInference × Claude Code Setup${NC}\n\n"

if command -v claude &>/dev/null; then
    CLAUDE_VERSION=$(claude --version 2>/dev/null || echo "unknown")
    ok "Claude Code detected (${CLAUDE_VERSION})"
else
    err "Claude Code is not installed."
    info "Install it with:  curl -fsSL https://claude.ai/install.sh | bash"
    info "Official instructions: https://code.claude.com/docs/en/installation"
    info "Then re-run this script."
    exit 1
fi

# ── 2. Prompt for API key ────────────────────────────────────────
printf "\n"
if [[ -n "${FREEINFERENCE_API_KEY:-}" ]]; then
    info "Using API key from \$FREEINFERENCE_API_KEY environment variable."
    API_KEY="$FREEINFERENCE_API_KEY"
else
    printf "${BOLD}Enter your FreeInference API key: ${NC}"
    read -r API_KEY
    if [[ -z "$API_KEY" ]]; then
        err "API key cannot be empty."
        exit 1
    fi
fi

# ── 3. Merge settings into ~/.claude/settings.json ───────────────
info "Configuring ${SETTINGS_FILE} ..."

mkdir -p "$(dirname "$SETTINGS_FILE")"

if command -v python3 &>/dev/null; then
    # Use Python for safe JSON merge (preserves existing settings)
    export _FI_BASE_URL="$FREEINFERENCE_BASE_URL"
    export _FI_API_KEY="$API_KEY"
    export _FI_MODEL="$FREEINFERENCE_MODEL"
    export _FI_HAIKU_MODEL="$FREEINFERENCE_HAIKU_MODEL"
    python3 << 'PYEOF'
import json
import os
import sys

settings_path = os.path.expanduser("~/.claude/settings.json")
new_env = {
    "ANTHROPIC_BASE_URL": os.environ.get("_FI_BASE_URL", ""),
    "ANTHROPIC_AUTH_TOKEN": os.environ.get("_FI_API_KEY", ""),
    "ANTHROPIC_DEFAULT_OPUS_MODEL": os.environ.get("_FI_MODEL", ""),
    "ANTHROPIC_DEFAULT_SONNET_MODEL": os.environ.get("_FI_MODEL", ""),
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": os.environ.get("_FI_HAIKU_MODEL", ""),
}

# Read existing settings or start fresh
if os.path.isfile(settings_path):
    with open(settings_path) as f:
        try:
            settings = json.load(f)
        except json.JSONDecodeError as exc:
            sys.exit(f"Refusing to overwrite invalid JSON in {settings_path}: {exc}")
else:
    settings = {}

if not isinstance(settings, dict):
    sys.exit(f"Refusing to overwrite non-object JSON in {settings_path}")

# Merge model and env settings (preserves unrelated user settings/env vars).
# Remove keys written by older versions of this setup script so they cannot
# override the current top-level/family model settings.
settings["model"] = os.environ.get("_FI_MODEL", "")
if not isinstance(settings.get("env"), dict):
    settings["env"] = {}
settings["env"].pop("ANTHROPIC_MODEL", None)
settings["env"].pop("ANTHROPIC_SMALL_FAST_MODEL", None)
if settings["env"].get("API_TIMEOUT_MS") == "600000":
    settings["env"].pop("API_TIMEOUT_MS")
settings["env"].update(new_env)

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF

elif command -v jq &>/dev/null; then
    # Fallback: use jq (--arg for safe escaping, no string interpolation)
    if [[ -f "$SETTINGS_FILE" ]]; then
        EXISTING=$(cat "$SETTINGS_FILE")
    else
        EXISTING='{}'
    fi
    TMP_SETTINGS=$(mktemp "${SETTINGS_FILE}.tmp.XXXXXX")
    if echo "$EXISTING" | jq \
        --arg base "$FREEINFERENCE_BASE_URL" \
        --arg key "$API_KEY" \
        --arg model "$FREEINFERENCE_MODEL" \
        --arg haiku "$FREEINFERENCE_HAIKU_MODEL" \
        '.model = $model
         | .env = (if (.env | type) == "object" then .env else {} end)
         | del(.env.ANTHROPIC_MODEL, .env.ANTHROPIC_SMALL_FAST_MODEL)
         | if .env.API_TIMEOUT_MS == "600000" then del(.env.API_TIMEOUT_MS) else . end
         | .env *= {"ANTHROPIC_BASE_URL": $base, "ANTHROPIC_AUTH_TOKEN": $key, "ANTHROPIC_DEFAULT_OPUS_MODEL": $model, "ANTHROPIC_DEFAULT_SONNET_MODEL": $model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": $haiku}' \
        > "$TMP_SETTINGS"; then
        mv "$TMP_SETTINGS" "$SETTINGS_FILE"
    else
        rm -f "$TMP_SETTINGS"
        err "Refusing to overwrite invalid JSON in ${SETTINGS_FILE}."
        exit 1
    fi

else
    err "python3 or jq is required to update ${SETTINGS_FILE} safely."
    exit 1
fi

ok "Settings written to ${SETTINGS_FILE}"

# ── 4. Connectivity test ─────────────────────────────────────────
printf "\n"
info "Testing connectivity to FreeInference API ..."

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$TEST_ENDPOINT" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${API_KEY}" \
    -d "{\"model\":\"${FREEINFERENCE_MODEL}\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" \
    --connect-timeout 10 \
    --max-time 30 \
    2>/dev/null || echo "000")

if [[ "$HTTP_CODE" == "000" ]]; then
    warn "Could not reach ${TEST_ENDPOINT} — check your network."
    warn "Configuration was saved. You can test later by running: claude"
elif [[ "$HTTP_CODE" =~ ^(200|201)$ ]]; then
    ok "API is reachable — connection successful!"
elif [[ "$HTTP_CODE" == "401" ]]; then
    warn "API returned 401 — double-check your API key."
    warn "Configuration was saved. Fix your key in ${SETTINGS_FILE} if needed."
elif [[ "$HTTP_CODE" == "429" ]]; then
    ok "API is reachable (rate limited right now, but your key works)."
else
    warn "API returned HTTP ${HTTP_CODE}. Configuration saved — you can debug later."
fi

# ── 5. Done ──────────────────────────────────────────────────────
printf "\n${GREEN}${BOLD}All set!${NC}\n\n"
info "Run ${BOLD}claude${NC} in any project directory to start coding."
info "Configured model: ${BOLD}${FREEINFERENCE_MODEL}${NC} (Haiku/background: ${BOLD}${FREEINFERENCE_HAIKU_MODEL}${NC})"
info "Other public models you can choose in settings or with /model:"
printf "    • ${BOLD}minimax-m3${NC}  (default)\n"
printf "    • ${BOLD}deepseek-v4-flash${NC}, ${BOLD}glm-5.1${NC}, ${BOLD}qwen3.6-35b${NC}\n"
printf "    • ${BOLD}minimax-m2.5${NC}, ${BOLD}diffusiongemma${NC}\n"
printf "    See https://freeinference.org/v1/models for the full list.\n"
printf "\n"
info "To change settings later, edit: ${SETTINGS_FILE}"
info "To uninstall, remove the FreeInference keys and model from that file.\n"
