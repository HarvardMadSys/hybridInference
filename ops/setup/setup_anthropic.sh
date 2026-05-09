#!/usr/bin/env bash
# Configure environment + Claude Code CLI to point at freeinference's
# Anthropic Messages compat surface.
#
# Both endpoints are supported and reach the same handler:
#   - https://freeinference.org/v1/messages           (recommended)
#   - https://freeinference.org/anthropic/v1/messages (legacy alias)

# Set ANTHROPIC_API_KEY in your shell or .env before running this script.
# Generate a key at https://freeinference.org/dashboard
: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY must be set (e.g. export ANTHROPIC_API_KEY=hyi-...)}"
export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_API_KEY}"
export ANTHROPIC_BASE_URL="https://freeinference.org"

mv ~/.claude ~/.claude.backup.$(date +%Y%m%d%H%M%S) 2>/dev/null || true
mv ~/.claude.json ~/.claude.json.backup.$(date +%Y%m%d%H%M%S) 2>/dev/null || true

mkdir -p ~/.claude
cat > ~/.claude/settings.json <<EOF
{
  "env": {
    "ANTHROPIC_BASE_URL": "${ANTHROPIC_BASE_URL}",
    "ANTHROPIC_AUTH_TOKEN": "${ANTHROPIC_AUTH_TOKEN}",
    "ANTHROPIC_MODEL": "${CLAUDE_MODEL}"
  },
  "hasCompletedOnboarding": true
}
EOF

# Sanity check: list models from freeinference
curl -s "${ANTHROPIC_BASE_URL}/v1/models" \
  -H "Authorization: Bearer ${ANTHROPIC_API_KEY}" \
  -H "anthropic-version: 2023-06-01" | head -c 500
echo
