#!/usr/bin/env bash
#
# Deploy the production Docker Compose stack from the canonical main branch.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/srv/hybridInference}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/health}"
FRONTEND_HEALTH_URL="${FRONTEND_HEALTH_URL:-http://127.0.0.1:3001/}"
TARGET_BRANCH="${TARGET_BRANCH:-main}"
DEPLOY_SHA="${DEPLOY_SHA:-}"
COMPOSE=(docker compose -f deploy/docker/docker-compose.yml --env-file .env)

log() {
  printf '[deploy] %s\n' "$*"
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

# Wrap body in a function so bash parses the entire script into memory before
# executing any command. The script self-modifies via `git reset --hard` below;
# without this guard, bash continues reading the disk file at the byte offset
# it stopped at, which silently desynchronizes once line counts shift.
main() {
  cd "$APP_DIR"

  if [[ ! -f .env ]]; then
    log "Missing ${APP_DIR}/.env; production secrets must stay on the server."
    exit 1
  fi

  # The canonical-commit reset below (git reset --hard) discards any tracked
  # local changes regardless, so refusing here only ever bricked the deploy:
  # innocuous working-tree drift on the server (e.g. a tracked file deleted
  # out-of-band) blocked the very reset that would have healed it. Back any
  # local changes up to a timestamped patch first, so an uncommitted operator
  # hotfix stays recoverable, then proceed.
  if ! git diff --quiet || ! git diff --cached --quiet; then
    backup="${APP_DIR}/.deploy-local-changes-$(date -u +%Y%m%dT%H%M%SZ).patch"
    log "Tracked local changes detected; backing up to ${backup} before reset."
    git status --short --untracked-files=no
    git diff HEAD > "$backup" || true
  fi

  log "Fetching origin/${TARGET_BRANCH}."
  git fetch --prune origin "$TARGET_BRANCH"

  if [[ -n "$DEPLOY_SHA" ]]; then
    # Only origin/${TARGET_BRANCH} was fetched above, so a SHA from any other
    # branch (e.g. a 'dev'-only commit dispatched against the wrong branch)
    # won't exist in this repo. Guard rev-parse so that case fails with a
    # clear message instead of a raw "unknown revision" git error (exit 128).
    if ! target_sha="$(git rev-parse --verify --quiet --end-of-options "${DEPLOY_SHA}^{commit}")"; then
      log "Refusing to deploy ${DEPLOY_SHA}; it is not on origin/${TARGET_BRANCH}."
      log "Production only deploys commits promoted to '${TARGET_BRANCH}'. If you"
      log "dispatched Deploy Production against another branch (e.g. 'dev'), re-run"
      log "it against '${TARGET_BRANCH}', or use Deploy Staging to deploy 'dev'."
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
  for attempt in $(seq 1 30); do
    if curl -fsS --max-time 5 --output /dev/null "$FRONTEND_HEALTH_URL"; then
      break
    fi
    if [[ "$attempt" -eq 30 ]]; then
      log "Frontend healthcheck failed after 30 attempts."
      exit 1
    fi
    sleep 5
  done

  log "Production deployment completed."
}

main "$@"
