#!/usr/bin/env bash
# Prove the export on the artifact, and prove the audit can fail.
#
# Two halves. First: build a fresh export tree and run every gate a stranger
# who cloned it would run — a locked dependency install, the default test
# suite, and the frontend's own lint/test/build. Second: plant each thing the
# audit exists to catch and show it returning non-zero. A scanner nobody has
# seen fail is a scanner nobody should trust.
set -uo pipefail

SRC="$(git rev-parse --show-toplevel)"
OUT="${1:?usage: prove_export.sh <empty-dir>}"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAILED: %s\033[0m\n' "$*"; exit 1; }

[[ -e "$OUT" ]] && fail "$OUT exists; give me a path I created"

say "Materialise"
(cd "$SRC" && uv run python ops/release/public_export.py --materialize "$OUT") || fail "export"

say "Export: uv sync --frozen"
(cd "$OUT" && uv venv -p 3.12 -q && uv sync --frozen -q) || fail "uv sync --frozen"

say "Export: default pytest"
(cd "$OUT" && PYTHONPATH=apps/backend .venv/bin/python -m pytest -q \
    -m "not external and not dbtest" -p no:randomly) || fail "pytest"

say "Export: npm ci / lint / test / build"
(cd "$OUT/apps/frontend" && npm ci --silent && npm run lint && npm test && npm run build) \
    || fail "frontend"

# ── The audit has to fail on each of these ─────────────────────────────
probe() {
    local what="$1"; shift
    local dir="${OUT}.probe"
    rm -rf "$dir"; cp -R "$SRC" "$dir" 2>/dev/null
    ( cd "$dir" && "$@" ) || { rm -rf "$dir"; fail "could not set up probe: $what"; }
    ( cd "$dir" && rm -rf ./.export-probe \
        && uv run python ops/release/public_export.py --materialize ./.export-probe >/dev/null 2>&1 )
    local rc=$?
    rm -rf "$dir"
    [[ $rc -ne 0 ]] || fail "$what did NOT make the audit fail"
    printf '  non-zero on: %s\n' "$what"
}

say "Audit fails on what it exists to catch"
probe "a planted gateway key" \
    bash -c 'printf "KEY=%s\n" "hyi-$(printf C%.0s $(seq 24))" >> README.md'
probe "a planted Slack bot token" \
    bash -c 'printf "TOKEN=%s\n" "xoxb-0123456789-planted" >> README.md'
probe "a planted cluster path" \
    bash -c 'printf "DIR=/scratch/someone/models\n" >> README.md'
probe "a planted internal hostname" \
    bash -c 'printf "ssh spark2 uptime\n" >> README.md'
probe "a secret hidden in an .svg" \
    bash -c 'mkdir -p apps/frontend/public && printf "<svg><!-- hyi-%s --></svg>" "$(printf D%.0s $(seq 24))" > apps/frontend/public/probe.svg && git add -A'
probe "a missing overlay source" \
    bash -c 'rm -f config/examples/models.openrouter.yaml'

say "Export proven."
