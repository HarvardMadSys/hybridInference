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
# Duplicated from deploy_production.sh rather than sourced: these scripts
# `git reset --hard` themselves mid-run, so a sourced helper would be read
# from whichever revision happened to be on disk at the time.
check_pgadmin_route() {
  local url="${FRONTEND_HEALTH_URL%/}/pgadmin"
  local status

  if [[ "$(docker inspect -f '{{.State.Running}}' hybridinference-pgadmin 2>/dev/null || true)" != "true" ]]; then
    log "WARNING: pgAdmin is not running, so the console's pgAdmin link will fail."
    log "WARNING: it needs the 'admin' Compose profile — see docs/developer/deployment.md."
    return 0
  fi

  log "Checking that the pgAdmin route refuses an anonymous request."
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url" || echo 000)"
  if [[ "$status" == "200" ]]; then
    log "FAILED: ${url} answered 200 without a session; the admin gate is not in front of pgAdmin."
    exit 1
  fi
  log "pgAdmin route refused an anonymous request with ${status}, as expected."
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

  # Cloud-agent runner (issue #1041): the host opts in by setting
  # AGENT_DISPATCHER_TOKEN in .env — the same variable the runner and the
  # gateway's claim gate already share, so there is no second switch to
  # forget. The overlay must ride this same compose invocation (it attaches
  # `backend` to the agent-egress network); with no token the deploy is
  # exactly what it was before this block existed.
  AGENT_RUNNER=0
  if grep -qE '^AGENT_DISPATCHER_TOKEN=..+' .env; then
    AGENT_RUNNER=1
    # Appended, not spliced: compose accepts flags in any order before the
    # subcommand, and what decides overlay precedence is the relative order of
    # the `-f` flags among themselves — which stays base-then-overlay here.
    COMPOSE+=(-f deploy/docker/docker-compose.agent-runner.yml)
    # The overlay refuses to start without an image name (an unqualified
    # default would resolve through Docker Hub). The deploy builds exactly
    # this tag below, so the name always resolves locally.
    sandbox_image="$(grep -E '^AGENT_SANDBOX_IMAGE=..+' .env | tail -1 | cut -d= -f2- || true)"
    export AGENT_SANDBOX_IMAGE="${sandbox_image:-hybridinference-agent-sandbox:latest}"
    log "Agent runner enabled (AGENT_DISPATCHER_TOKEN is set); sandbox image ${AGENT_SANDBOX_IMAGE}."
    # Name this machine for the admin host switch. Resolved out here because
    # the runner container's own hostname is a container id, so it cannot
    # answer "which machine am I" for itself.
    if ! grep -qE '^AGENT_RUNNER_HOST=..+' .env; then
      export AGENT_RUNNER_HOST="${AGENT_RUNNER_HOST:-$(hostname -s 2>/dev/null || hostname)}"
      log "Runner host pool entry: ${AGENT_RUNNER_HOST} (set AGENT_RUNNER_HOST in .env to rename)."
    fi
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

  if [[ "$AGENT_RUNNER" == "1" ]]; then
    log "Building the agent sandbox image (${AGENT_SANDBOX_IMAGE})."
    docker build -f deploy/docker/Dockerfile.agent-sandbox -t "$AGENT_SANDBOX_IMAGE" .

    # Gate the isolation boundary before the stack comes up. Delegated to the
    # runner script rather than repeated here so there is one definition of
    # "this host is fit to run sandboxes" — it reads AGENT_SANDBOX_BACKEND the
    # way compose does, requires the Kata shim when that backend is `kata`, and
    # proves VM isolation by starting one container from the image just built
    # and checking it does not report the host's kernel.
    #
    # After the image build because the proof needs the image; before
    # `make build` because a host that cannot isolate should not get runners.
    log "Preflighting the sandbox isolation boundary."
    if ! ops/deploy/agent_runner.sh preflight; then
      log "Refusing to deploy the agent runner: this host cannot provide the"
      log "isolation its configuration claims. Fix the host, or set"
      log "AGENT_SANDBOX_BACKEND=container in .env to accept a shared kernel."
      exit 1
    fi
  fi

  log "Rebuilding and restarting Docker Compose services."
  # The rebuild needs this site's identity too: the console's is compiled in
  # as build args. Make builds its own Compose command, so pass the staging
  # files explicitly rather than relying on the diagnostic COMPOSE array above.
  make build DISTRIBUTION=freeinference AGENT_RUNNER="$AGENT_RUNNER" \
    COMPOSE_EXTRA_ENV_FILES='distributions/freeinference/deploy/staging/*.env'

  log "Current service state:"
  "${COMPOSE[@]}" ps

  log "Checking backend health at ${HEALTH_URL}."
  curl -fsS --retry 30 --retry-delay 5 --retry-connrefused "$HEALTH_URL"
  printf '\n'

  log "Checking frontend health at ${FRONTEND_HEALTH_URL}."
  curl -fsS --retry 30 --retry-delay 5 --retry-connrefused --retry-all-errors --output /dev/null \
    "$FRONTEND_HEALTH_URL"

  check_pgadmin_route

  log "Staging deployment completed."
}

main "$@"
