#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────
# FreeInference — Claude Code one-click setup script
#
# Usage:
#   # Recommended: download first, inspect, then run
#   curl -fsSL -o setup_claude_code.sh https://raw.githubusercontent.com/HarvardMadSys/hybridInference/main/ops/setup/setup_claude_code.sh
#   bash setup_claude_code.sh
#
#   # Or from a cloned repo:
#   bash ops/setup/setup_claude_code.sh
#
# What it does:
#   1. Checks that Claude Code (claude) is installed
#   2. Asks for your FreeInference API key
#   3. Merges the required env vars into ~/.claude/settings.json
#   4. Runs a quick connectivity test against the proxy
# ──────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Configurable defaults ────────────────────────────────────────
FREEINFERENCE_BASE_URL="https://freeinference.org/anthropic"
# Claude Code's built-in Anthropic model defaults are not part of the public
# catalog, so we pin public models. Override via env if you have access to others.
FREEINFERENCE_MODEL="${FREEINFERENCE_MODEL:-glm-5.1}"
FREEINFERENCE_SMALL_FAST_MODEL="${FREEINFERENCE_SMALL_FAST_MODEL:-glm-5-turbo}"
SETTINGS_FILE="${HOME}/.claude/settings.json"
API_TIMEOUT_MS="600000"
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
    info "Install it with:  npm install -g @anthropic-ai/claude-code"
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
    export _FI_TIMEOUT="$API_TIMEOUT_MS"
    export _FI_MODEL="$FREEINFERENCE_MODEL"
    export _FI_SMALL_MODEL="$FREEINFERENCE_SMALL_FAST_MODEL"
    python3 << 'PYEOF'
import json, os, sys

settings_path = os.path.expanduser("~/.claude/settings.json")
new_env = {
    "ANTHROPIC_BASE_URL": os.environ.get("_FI_BASE_URL", ""),
    "ANTHROPIC_AUTH_TOKEN": os.environ.get("_FI_API_KEY", ""),
    "ANTHROPIC_MODEL": os.environ.get("_FI_MODEL", ""),
    "ANTHROPIC_SMALL_FAST_MODEL": os.environ.get("_FI_SMALL_MODEL", ""),
    "API_TIMEOUT_MS": os.environ.get("_FI_TIMEOUT", ""),
}

# Read existing settings or start fresh
if os.path.isfile(settings_path):
    with open(settings_path) as f:
        try:
            settings = json.load(f)
        except json.JSONDecodeError:
            settings = {}
else:
    settings = {}

# Merge env block (preserves other env vars the user may have set)
if "env" not in settings:
    settings["env"] = {}
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
    echo "$EXISTING" | jq \
        --arg base "$FREEINFERENCE_BASE_URL" \
        --arg key "$API_KEY" \
        --arg model "$FREEINFERENCE_MODEL" \
        --arg smallmodel "$FREEINFERENCE_SMALL_FAST_MODEL" \
        --arg timeout "$API_TIMEOUT_MS" \
        '.env = (.env // {} | . * {"ANTHROPIC_BASE_URL": $base, "ANTHROPIC_AUTH_TOKEN": $key, "ANTHROPIC_MODEL": $model, "ANTHROPIC_SMALL_FAST_MODEL": $smallmodel, "API_TIMEOUT_MS": $timeout})' \
        > "$SETTINGS_FILE"

else
    # Last resort: write directly (will overwrite existing settings)
    warn "Neither python3 nor jq found — writing settings from scratch."
    warn "If you had existing settings in ${SETTINGS_FILE}, they may be overwritten."
    cat > "$SETTINGS_FILE" <<EOF
{
  "env": {
    "ANTHROPIC_BASE_URL": "${FREEINFERENCE_BASE_URL}",
    "ANTHROPIC_AUTH_TOKEN": "${API_KEY}",
    "ANTHROPIC_MODEL": "${FREEINFERENCE_MODEL}",
    "ANTHROPIC_SMALL_FAST_MODEL": "${FREEINFERENCE_SMALL_FAST_MODEL}",
    "API_TIMEOUT_MS": "${API_TIMEOUT_MS}"
  }
}
EOF
fi

ok "Settings written to ${SETTINGS_FILE}"

# ── 3b. Shell profile export for ANTHROPIC_BASE_URL ─────────────
# Claude Code >= 2.1.198 no longer applies ANTHROPIC_BASE_URL from the
# settings.json env block (it withholds API-routing variables and falls back
# to api.anthropic.com, which rejects FreeInference keys with
# "401 Invalid bearer token"). A process-level environment variable still
# works, so the base URL is also exported from the shell profile. The auth
# token stays in settings.json only — no secrets are written to the profile.
RC_MARKER_BEGIN="# >>> freeinference claude-code >>>"
RC_MARKER_END="# <<< freeinference claude-code <<<"

shell_profile_for() {
    case "$(basename "${SHELL:-/bin/bash}")" in
        zsh)  echo "${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash) echo "$HOME/.bashrc" ;;
        *)    echo "" ;;
    esac
}

RC_FILE=$(shell_profile_for)
if [[ -n "$RC_FILE" ]]; then
    # Replace any previous block so re-runs stay idempotent.
    if [[ -f "$RC_FILE" ]] && grep -qF "$RC_MARKER_BEGIN" "$RC_FILE"; then
        TMP_RC=$(mktemp)
        awk -v begin="$RC_MARKER_BEGIN" -v end="$RC_MARKER_END" \
            '$0 == begin {skip=1; next} $0 == end {skip=0; next} !skip' \
            "$RC_FILE" > "$TMP_RC" && mv "$TMP_RC" "$RC_FILE"
    fi
    {
        printf '%s\n' "$RC_MARKER_BEGIN"
        printf 'export ANTHROPIC_BASE_URL="%s"\n' "$FREEINFERENCE_BASE_URL"
        printf '%s\n' "$RC_MARKER_END"
    } >> "$RC_FILE"
    ok "Exported ANTHROPIC_BASE_URL in ${RC_FILE} (required by Claude Code >= 2.1.198)"
    info "Open a new terminal (or run: source ${RC_FILE}) before starting claude."
else
    warn "Unrecognized shell '$(basename "${SHELL:-unknown}")' — add this line to your shell profile:"
    printf '    export ANTHROPIC_BASE_URL="%s"\n' "$FREEINFERENCE_BASE_URL"
fi

# ── 4. Connectivity test ─────────────────────────────────────────
printf "\n"
info "Testing connectivity to FreeInference API ..."

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$TEST_ENDPOINT" \
    -H "Content-Type: application/json" \
    -H "x-api-key: ${API_KEY}" \
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
info "Configured model: ${BOLD}${FREEINFERENCE_MODEL}${NC} (small/fast: ${BOLD}${FREEINFERENCE_SMALL_FAST_MODEL}${NC})"
info "Other public models you can set via ANTHROPIC_MODEL:"
printf "    • ${BOLD}glm-5.1${NC}  (default)\n"
printf "    • ${BOLD}glm-5-turbo${NC}, ${BOLD}minimax-m2.5${NC}\n"
printf "    See https://freeinference.org/v1/models for the full list.\n"
printf "\n"
info "To change settings later, edit: ${SETTINGS_FILE}"
info "To uninstall, remove the env block from that file.\n"
