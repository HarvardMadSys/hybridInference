#!/usr/bin/env bash
#
# Stand up (or scale, or stop) the self-hosted agent-job runner as a STANDING
# service — issue #1041's missing P0.5. The control plane has been deployed
# and the runner code merged for a while; what never existed was a runner that
# stays up, so every queued job waited for someone to start one by hand on a
# laptop. This script is the deployment artifact that fixes that: compose's
# `restart: unless-stopped` plus an enabled Docker daemon is the standing
# mechanism, so "deployed" means "run this once per host".
#
# Usage, from the repository root on the runner host:
#
#   ops/deploy/agent_runner.sh up [N]     # build images, start N runners (default 1)
#   ops/deploy/agent_runner.sh status     # what is running, and the recent log tail
#   ops/deploy/agent_runner.sh down       # stop the runners (main stack untouched)
#
# The runner claims work over HTTP, so this host needs nothing from the
# gateway host except network reach; scaling is `up N` because claim_job uses
# FOR UPDATE SKIP LOCKED — runners share one queue with no coordination.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-$(pwd)}"
cd "$APP_DIR"

COMPOSE=(docker compose
  -f deploy/docker/docker-compose.yml
  -f deploy/docker/docker-compose.agent-runner.yml)
for env_file in "$APP_DIR"/distributions/*/deploy/*.env; do
  [[ -f "$env_file" ]] && COMPOSE+=(--env-file "$env_file")
done
COMPOSE+=(--env-file .env)

SANDBOX_IMAGE="${AGENT_SANDBOX_IMAGE:-hybridinference-agent-sandbox:latest}"

log() { printf '[agent-runner] %s\n' "$*"; }
die() { printf '[agent-runner] ERROR: %s\n' "$*" >&2; exit 1; }

require_env() {
  # Fail here with names, not later with one job failing at a time.
  local missing=()
  grep -q '^AGENT_DISPATCHER_TOKEN=..*' .env 2>/dev/null \
    || [[ -n "${AGENT_DISPATCHER_TOKEN:-}" ]] || missing+=(AGENT_DISPATCHER_TOKEN)
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "missing in .env: ${missing[*]} (see docs/developer/agent-sandbox-operations.md)"
  fi
}

cmd_up() {
  # Declared, not passed as --scale: the flag survives only until the next
  # `docker compose up` without it, which drops the fleet back to one and
  # reads as "everyone is queueing" long afterwards. One runner takes one job
  # at a time, so this number is the deployment's job concurrency.
  local replicas="${1:-${AGENT_RUNNER_REPLICAS:-3}}"
  [[ "$replicas" =~ ^[0-9]+$ ]] || die "replicas must be a number, got '$replicas'"
  command -v docker >/dev/null || die "docker is not installed on this host"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running (or not accessible)"
  require_env

  log "building the sandbox image (${SANDBOX_IMAGE})"
  docker build -f deploy/docker/Dockerfile.agent-sandbox -t "$SANDBOX_IMAGE" .

  log "starting ${replicas} runner(s); restart policy keeps them up across reboots"
  log "persist it with AGENT_RUNNER_REPLICAS=${replicas} in .env, or the next deploy uses the default"
  # The proxy is named explicitly rather than left to `depends_on`. Compose does
  # start a dependency, but this is the deployment artifact: an operator reading
  # it should see that the setup phase's egress is a service on this host, and
  # `status`/`down` below have to name it anyway.
  AGENT_SANDBOX_IMAGE="$SANDBOX_IMAGE" AGENT_RUNNER_REPLICAS="$replicas" \
    "${COMPOSE[@]}" up -d --build agent-egress-proxy agent-runner

  # Preflight (backend reachable, image spawnable, workdir bind-mountable,
  # egress networks internal, and the setup proxy actually refusing a host that
  # is not on its allowlist) runs before the runner claims anything; surface its
  # verdict here instead of leaving it in a detached log.
  sleep 3
  "${COMPOSE[@]}" logs --tail 20 agent-runner
  log "done. 'ops/deploy/agent_runner.sh status' shows the live tail."
}

cmd_status() {
  "${COMPOSE[@]}" ps agent-runner agent-egress-proxy
  "${COMPOSE[@]}" logs --tail 40 agent-runner
  # Denied requests are only visible here: an internal network refuses a
  # connection silently, so the proxy's log is the one record that a job tried
  # to reach something it may not.
  "${COMPOSE[@]}" logs --tail 20 agent-egress-proxy
}

cmd_down() {
  "${COMPOSE[@]}" rm --stop --force agent-runner agent-egress-proxy
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  status) cmd_status ;;
  down) cmd_down ;;
  *) die "usage: $0 up [replicas] | status | down" ;;
esac
