#!/usr/bin/env bash
#
# Deploy the staging Docker Compose stack from the dev branch.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/srv/hybridInference}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/health}"
FRONTEND_HEALTH_URL="${FRONTEND_HEALTH_URL:-http://127.0.0.1:3001/}"
TARGET_BRANCH="${TARGET_BRANCH:-dev}"
DEPLOY_SHA="${DEPLOY_SHA:-}"
# This deployment's public identity — site name, links, CORS, console build
# args — lives in the distribution overlay, because the upstream defaults name
# no deployment. Without these files the stack would come up unbranded. `.env`
# is passed last so it wins, keeping per-host overrides working; secrets live
# only in `.env`, never in the checked-in overlay.
COMPOSE=(docker compose -f deploy/docker/docker-compose.yml)
for env_file in "$APP_DIR"/distributions/freeinference/deploy/*.env; do
  if [[ -f "$env_file" ]]; then
    COMPOSE+=(--env-file "$env_file")
  fi
done
# The files above are the site's, and their values are production's. Anything
# that has to differ on staging belongs in deploy/staging/, which is read after
# them and before `.env` — so it can be reviewed in the repository rather than
# living only on the host.
for env_file in "$APP_DIR"/distributions/freeinference/deploy/staging/*.env; do
  if [[ -f "$env_file" ]]; then
    COMPOSE+=(--env-file "$env_file")
  fi
done
COMPOSE+=(--env-file .env)

log() {
  printf '[deploy-staging] %s\n' "$*"
}

dump_diagnostics() {
  local status=$?
  if [[ "$status" -ne 0 ]]; then
    log "Deployment failed with exit status ${status}; collecting diagnostics."
    "${COMPOSE[@]}" ps || true
    "${COMPOSE[@]}" logs --tail=100 backend frontend || true
  fi
  exit "$status"
}

trap dump_diagnostics EXIT

# pgAdmin is reached through the console, which gates it on an admin session
# (apps/frontend/src/app/pgadmin/). Two things can go wrong here without
# anything looking broken, and the second one went unnoticed for three months:
#
#   1. pgAdmin is not running — the link 502s. Loud, harmless, warn only.
#   2. the gate is not in front of it — an anonymous request gets a database
#      console. Nothing in a deployment reports this on its own, so fail.
#
# Hand Compose its profile selection through the process environment.
#
# The overlay states it in an env file, which is where a deployment's choices
# belong — but that is not a reliable transport for this one variable, twice
# over. Compose ignored COMPOSE_PROFILES inside `--env-file` from 2.27.1 until
# the fix for docker/compose#11856, and these scripts pass even the host's own
# `.env` that way. And `--profile` on the command line is not a substitute:
# compose-go's WithDefaultProfiles drops COMPOSE_PROFILES entirely once any
# profile is passed explicitly, so a flag would silently switch off whatever a
# host had selected for itself.
#
# The process environment is honoured by every version, so resolve the union
# here: the overlay's selection plus the host's own. Runs after the checkout,
# so it reads the revision being deployed rather than the one on disk.
export_compose_profiles() {
  local overlay host combined
  overlay="$(read_compose_profiles distributions/freeinference/deploy/compose.env)"
  host="$(read_compose_profiles .env)"
  combined="${overlay}${overlay:+${host:+,}}${host}"

  if [[ -n "$combined" ]]; then
    export COMPOSE_PROFILES="$combined"
    log "Compose profiles: ${COMPOSE_PROFILES}."
  fi
}

read_compose_profiles() {
  [[ -f "$1" ]] || return 0
  sed -n 's/^[[:space:]]*COMPOSE_PROFILES=//p' "$1" | tail -1 | tr -d "\"'"
}

# Read one value from a dotenv file the way Compose reads it.
#
# **The quotes are the whole point.** `AGENT_NETWORK_NAME="cloud-agent"` is
# valid dotenv, and Compose strips those quotes when it resolves the overlay —
# so a reader that keeps them asks Docker about a network named `"cloud-agent"`,
# is told it does not exist, and quietly skips the attachment. The network is
# right there; the deploy just never looks at it. Same normalisation as
# `read_compose_profiles` above, which is where this shape comes from.
read_env_value() {
  [[ -f "$2" ]] || return 0
  sed -n "s/^[[:space:]]*$1=//p" "$2" | tail -1 | tr -d "\"'"
}

# An anonymous GET has one correct answer — a 302 to the login page — and the
# check asserts exactly that. "Anything but 200" would not do: the outage this
# exists for is the console answering with its own 404, which is also non-200.
#
# Duplicated from deploy_production.sh rather than sourced: these scripts
# `git reset --hard` themselves mid-run, so a sourced helper would be read
# from whichever revision happened to be on disk at the time.
check_pgadmin_route() {
  local url="${FRONTEND_HEALTH_URL%/}/pgadmin"
  local probe code location

  if [[ "$(docker inspect -f '{{.State.Running}}' hybridinference-pgadmin 2>/dev/null || true)" != "true" ]]; then
    log "WARNING: pgAdmin is not running, so the console's pgAdmin link will fail."
    log "WARNING: it needs the 'admin' Compose profile — see docs/developer/deployment.md."
    return 0
  fi

  log "Checking that the pgAdmin route refuses an anonymous request."
  # Split by hand rather than with `read`: curl's -w output has no trailing
  # newline, read returns non-zero at EOF, and `set -e` turns that into an
  # exit that looks like the check passed.
  probe="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 10 "$url" || echo '000 ')"
  code="${probe%% *}"
  location="${probe#* }"
  if [[ "$code" != "302" || "$location" != */login ]]; then
    log "FAILED: an anonymous GET of ${url} must redirect to the login page."
    log "FAILED: got status '${code}', redirect '${location}'."
    log "FAILED: 404 means the console route is gone; 200 means nothing is gating pgAdmin."
    exit 1
  fi
  log "pgAdmin route redirected an anonymous request to the login page, as expected."
}

# Wrap body in a function so bash parses the entire script into memory before
# executing any command. The script self-modifies via `git reset --hard` below;
# without this guard, bash continues reading the disk file at the byte offset
# it stopped at, which silently desynchronizes once line counts shift.
main() {
  cd "$APP_DIR"

  if [[ ! -f .env ]]; then
    log "Missing ${APP_DIR}/.env; staging secrets must stay on the server."
    exit 1
  fi

  # The standalone cloud agent, if this host also runs it. The console's
  # `/agents` rewrites (#1206) are baked with Docker DNS names, so they only
  # resolve once the console is on that stack's network — and the network
  # existing is exactly the fact that says the stack is here. Detected rather
  # than configured: a second switch to set is a second switch to forget, and
  # forgetting it produces a 500 on a page the console still advertises.
  CLOUD_AGENT_NETWORK=0
  agent_network="$(read_env_value AGENT_NETWORK_NAME .env)"
  agent_network="${agent_network:-cloud-agent}"
  if docker network inspect "$agent_network" >/dev/null 2>&1; then
    CLOUD_AGENT_NETWORK=1
    COMPOSE+=(-f deploy/docker/docker-compose.cloud-agent.yml)
    log "Cloud agent network ${agent_network} found: the console will join it."
  fi

  # Refuse only when the working tree diverges from HEAD for tracked files,
  # i.e. an operator left an uncommitted hotfix worth preserving. Comparing
  # against HEAD (rather than also inspecting the staging index) is deliberate:
  # the `git reset --hard` below unconditionally discards staged state, so a
  # stray index entry -- e.g. a `git add`ed-then-deleted analysis script left
  # on the box -- must not permanently wedge deploys that the reset would
  # otherwise clean up on its own.
  if ! git diff --quiet HEAD --; then
    log "Refusing to deploy because tracked local changes exist."
    git status --short --untracked-files=no
    exit 1
  fi

  log "Fetching origin/${TARGET_BRANCH}."
  git fetch --prune origin "$TARGET_BRANCH"

  if [[ -n "$DEPLOY_SHA" ]]; then
    # Only origin/${TARGET_BRANCH} was fetched above, so a SHA from any other
    # branch won't exist in this repo. Guard rev-parse so that case fails with a
    # clear message instead of a raw "unknown revision" git error (exit 128).
    if ! target_sha="$(git rev-parse --verify --quiet --end-of-options "${DEPLOY_SHA}^{commit}")"; then
      log "Refusing to deploy ${DEPLOY_SHA}; it is not on origin/${TARGET_BRANCH}."
      log "Staging only deploys commits on '${TARGET_BRANCH}'. If you dispatched"
      log "Deploy Staging against another branch, re-run it against '${TARGET_BRANCH}'."
      exit 1
    fi
    if ! git merge-base --is-ancestor "$target_sha" "origin/${TARGET_BRANCH}"; then
      log "Refusing to deploy ${target_sha}; it is not on origin/${TARGET_BRANCH}."
      exit 1
    fi
  else
    target_sha="$(git rev-parse "origin/${TARGET_BRANCH}")"
  fi

  current_sha="$(git rev-parse HEAD)"
  log "Deploying ${target_sha} (current ${current_sha})."

  git reset --hard "$target_sha"
  git submodule update --init --recursive

  export BUILD_SHA="$target_sha"
  export BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  log "Build metadata: SHA=${BUILD_SHA} TIMESTAMP=${BUILD_TIMESTAMP}."

  log "Refreshing DB-IP Country Lite data (best effort)."
  if ! ops/setup/update_dbip_country_lite.sh; then
    log "WARNING: DB-IP Country Lite update failed; retaining the last good database."
  fi

  export_compose_profiles

  log "Rebuilding and restarting Docker Compose services."
  # The rebuild needs this site's identity too: the console's is compiled in
  # as build args. Make builds its own Compose command, so pass the staging
  # files explicitly rather than relying on the diagnostic COMPOSE array above.
  make build DISTRIBUTION=freeinference \
    CLOUD_AGENT_NETWORK="$CLOUD_AGENT_NETWORK" \
    COMPOSE_EXTRA_ENV_FILES='distributions/freeinference/deploy/staging/*.env'

  log "Current service state:"
  "${COMPOSE[@]}" ps

  # --retry-all-errors, not just --retry-connrefused: a published Docker port is
  # bound by docker-proxy the instant the container starts, so the connect
  # succeeds and the far end resets while the server inside is still coming up.
  # That is CURLE_RECV_ERROR (56), which --retry-connrefused does not cover, so
  # curl gives up on the first attempt and the --retry flags never engage. The
  # frontend line below already carries the flag; this one did not, and the same
  # asymmetry on the production script failed six deploys of a stack that came
  # up healthy anyway.
  log "Checking backend health at ${HEALTH_URL}."
  curl -fsS --retry 30 --retry-delay 5 --retry-connrefused --retry-all-errors "$HEALTH_URL"
  printf '\n'

  log "Checking frontend health at ${FRONTEND_HEALTH_URL}."
  curl -fsS --retry 30 --retry-delay 5 --retry-connrefused --retry-all-errors --output /dev/null \
    "$FRONTEND_HEALTH_URL"

  check_pgadmin_route

  log "Staging deployment completed."
}

main "$@"
