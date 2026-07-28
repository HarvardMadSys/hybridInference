#!/usr/bin/env bash
# Would merging the split change what this host runs?
#
# Run this ON the deployment host, before merging. It needs the host's .env,
# which is why it cannot be answered from a laptop or from CI.
#
#     ./ops/release/check_production_env.sh
#     APP_DIR=/srv/hybridInference ./ops/release/check_production_env.sh
#
# Reads values only to compare them; secrets are never printed.
#
# The reasoning it encodes:
#
#   Both before and after the split, `.env` is passed to compose last, so it
#   wins. Any key set there is therefore unaffected by the merge. What changes
#   is the fallback underneath it: before, a default compiled into
#   settings.py; after, the value in distributions/freeinference/deploy/*.env.
#   So the keys at risk are exactly the overlay keys this host does not set.
#
# One of those keys is not like the others. Alert records label their
# environment from DEPLOYMENT_ENV when set, and otherwise infer it from the
# BASE_URL host -- but only when BASE_URL was set explicitly. A host that
# relies on the built-in default is deliberately reported as `local`, so if
# this host does not set BASE_URL today its alerts say `local`, and after the
# merge they will say `production`. That is a more correct label and still a
# change: anything keyed on the environment field (dashboards, control-plane
# incident identity, alert filters) sees a new value on the same alert.
set -uo pipefail

APP_DIR="${APP_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
OVERLAY_DIR="${OVERLAY_DIR:-$APP_DIR/distributions/freeinference/deploy}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "No $ENV_FILE. Run this on the deployment host, or point ENV_FILE at its .env."
  exit 2
fi
if [[ ! -d "$OVERLAY_DIR" ]]; then
  echo "No $OVERLAY_DIR. Run this from a checkout that already has the overlay."
  exit 2
fi

# Names only. Values from .env are never read into a variable or printed.
host_keys=$(grep -hoE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=' "$ENV_FILE" \
            | tr -d ' \t=' | sort -u)

overlay_keys=$(cat "$OVERLAY_DIR"/*.env 2>/dev/null \
               | grep -hoE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=' \
               | tr -d ' \t=' | sort -u)

unset_keys=$(comm -23 <(printf '%s\n' "$overlay_keys") <(printf '%s\n' "$host_keys"))

echo "Host .env sets $(printf '%s\n' "$host_keys" | grep -c .) keys."
echo "Overlay supplies $(printf '%s\n' "$overlay_keys" | grep -c .) keys."
echo

if [[ -z "$unset_keys" ]]; then
  echo "This host sets every key the overlay supplies, and .env is passed last."
  echo "The merge changes nothing here."
else
  echo "The overlay would become the source for these, which this host does not set:"
  printf '  %s\n' $unset_keys
  echo
  echo "For each, the question is whether the overlay value matches the default"
  echo "the code uses today. Compare against distributions/freeinference/deploy/."
fi

echo
if printf '%s\n' "$host_keys" | grep -qx DEPLOYMENT_ENV; then
  echo "DEPLOYMENT_ENV: set here, so alert environment labels do not change."
elif printf '%s\n' "$host_keys" | grep -qx BASE_URL; then
  echo "DEPLOYMENT_ENV: not set here, but BASE_URL is — alerts already infer their"
  echo "  environment from its host, and the overlay sets the same host. No change."
else
  echo "DEPLOYMENT_ENV: not set, and neither is BASE_URL."
  echo "  Alerts from this host are currently labelled \`local\` — the code refuses to"
  echo "  assume production from a built-in default. After the merge they will say"
  echo "  \`production\`. Decide whether anything downstream keys on that field"
  echo "  before merging; set DEPLOYMENT_ENV in .env to keep today's label."
fi
