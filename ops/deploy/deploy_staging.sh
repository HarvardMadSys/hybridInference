#!/usr/bin/env bash
#
# Deploy the staging Docker Compose stack from the dev branch.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/srv/hybridInference}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/health}"
FRONTEND_HEALTH_URL="${FRONTEND_HEALTH_URL:-http://127.0.0.1:3001/}"
TARGET_BRANCH="${TARGET_BRANCH:-dev}"
DEPLOY_SHA="${DEPLOY_SHA:-}"
COMPOSE=(docker compose -f deploy/docker/docker-compose.yml --env-file .env)

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

cd "$APP_DIR"

if [[ ! -f .env ]]; then
  log "Missing ${APP_DIR}/.env; staging secrets must stay on the server."
  exit 1
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

log "Rebuilding and restarting Docker Compose services."
make build

log "Current service state:"
"${COMPOSE[@]}" ps

log "Checking backend health at ${HEALTH_URL}."
curl -fsS --retry 30 --retry-delay 5 --retry-connrefused "$HEALTH_URL"
printf '\n'

log "Checking frontend health at ${FRONTEND_HEALTH_URL}."
curl -fsS --retry 30 --retry-delay 5 --retry-connrefused --retry-all-errors --output /dev/null \
  "$FRONTEND_HEALTH_URL"

log "Staging deployment completed."
