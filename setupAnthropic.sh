#!/usr/bin/env bash
# Configure environment + Claude Code CLI to point at freeinference's
# Anthropic Messages compat surface.
#
# Both endpoints are supported and reach the same handler:
#   - https://staging.freeinference.org/v1/messages           (recommended)
#   - https://staging.freeinference.org/anthropic/v1/messages (legacy alias)

export ANTHROPIC_AUTH_TOKEN="hyi-JLL5SnXa14DpKSF3GcQQRmfAm9aDmjxEfod4X5EWrWk"
export ANTHROPIC_API_KEY="hyi-JLL5SnXa14DpKSF3GcQQRmfAm9aDmjxEfod4X5EWrWk"
export ANTHROPIC_BASE_URL="https://staging.freeinference.org"
export CLAUDE_MODEL="claude-opus-4.7"

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

# Sanity check: list models from staging.
curl -s "${ANTHROPIC_BASE_URL}/v1/models" \
  -H "Authorization: Bearer ${ANTHROPIC_API_KEY}" \
  -H "anthropic-version: 2023-06-01" | head -c 500
echo
