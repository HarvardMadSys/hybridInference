#!/usr/bin/env bash
#
# Stand up agent-job runners on a machine that is NOT the gateway's.
#
# `agent_runner.sh` assumes the two are colocated: it layers the runner overlay
# on the main compose file, whose `agent-runner` declares `depends_on: backend`.
# Point that at a machine with no gateway and it does not fail — it starts one,
# on a box that was only ever meant to run jobs. This script uses a standalone
# compose file with no backend in it at all.
#
# The piece that makes a remote host work is a tunnel container on both the
# closed agent network and a routable one, forwarding a single port to the
# gateway host over ssh. The sandbox's world stays what it always was: one
# endpoint, ours. Nothing here is reachable from the sandbox except that.
#
# Usage, from the repository root on the runner host:
#
#   ops/deploy/agent_remote_runner.sh up [N]   # build, start the tunnel + N runners
#   ops/deploy/agent_remote_runner.sh status   # what is running, and the log tails
#   ops/deploy/agent_remote_runner.sh check    # prove the tunnel reaches the gateway
#   ops/deploy/agent_remote_runner.sh down     # stop everything this file started
#
# Required in .env on this host:
#
#   AGENT_DISPATCHER_TOKEN         same value as on the gateway
#   AGENT_SANDBOX_IMAGE            image the sandboxes run
#   AGENT_TUNNEL_SSH_DESTINATION   user@gateway-host
#   AGENT_TUNNEL_SSH_KEY           path to this host's tunnel key (mounted ro)
#   AGENT_TUNNEL_SSH_KNOWN_HOSTS   path to a known_hosts holding the gateway's key
#
# Restrict the tunnel key on the gateway host — it needs one forwarded port and
# no shell:
#
#   command="",restrict,permitopen="127.0.0.1:8080" ssh-ed25519 AAAA... agent-tunnel

set -Eeuo pipefail

APP_DIR="${APP_DIR:-$(pwd)}"
cd "$APP_DIR"

COMPOSE=(docker compose
  -p hybridinference-agent-remote
  -f deploy/docker/docker-compose.agent-remote-runner.yml)
for env_file in "$APP_DIR"/distributions/*/deploy/*.env; do
  [[ -f "$env_file" ]] && COMPOSE+=(--env-file "$env_file")
done
COMPOSE+=(--env-file .env)

SANDBOX_IMAGE="${AGENT_SANDBOX_IMAGE:-hybridinference-agent-sandbox:latest}"
GATEWAY_HOSTNAME="${AGENT_GATEWAY_HOSTNAME:-agent-gateway}"
TUNNEL_PORT="${AGENT_TUNNEL_LISTEN_PORT:-8080}"

log() { printf '[agent-remote-runner] %s\n' "$*"; }
die() { printf '[agent-remote-runner] ERROR: %s\n' "$*" >&2; exit 1; }

env_or_dotenv() {
  # A value may come from the environment or from .env; compose reads both, so
  # the preflight has to look in both or it rejects working configurations.
  local name="$1"
  if [[ -n "${!name:-}" ]]; then
    printf '%s' "${!name}"
    return 0
  fi
  sed -n "s/^${name}=//p" .env 2>/dev/null | tail -1
}

require_env() {
  # Named up front, because each of these fails much later and much less
  # legibly: a missing token as a 401 per claim, a missing key as a container
  # that restarts forever, a missing known_hosts as a refusal nobody reads.
  local missing=() name
  for name in AGENT_DISPATCHER_TOKEN AGENT_SANDBOX_IMAGE \
              AGENT_TUNNEL_SSH_DESTINATION AGENT_TUNNEL_SSH_KEY \
              AGENT_TUNNEL_SSH_KNOWN_HOSTS; do
    [[ -n "$(env_or_dotenv "$name")" ]] || missing+=("$name")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "missing in .env: ${missing[*]} (see docs/developer/agent-sandbox-operations.md)"
  fi

  local key known
  key="$(env_or_dotenv AGENT_TUNNEL_SSH_KEY)"
  known="$(env_or_dotenv AGENT_TUNNEL_SSH_KNOWN_HOSTS)"
  [[ -r "$key" ]] || die "AGENT_TUNNEL_SSH_KEY=$key is not readable on this host"
  [[ -s "$known" ]] || die \
    "AGENT_TUNNEL_SSH_KNOWN_HOSTS=$known is missing or empty. Create it with
     'ssh-keyscan <gateway-host> > $known' and check the fingerprint before
     trusting it — this file is what stops the dispatcher credential going to
     whatever answers on that address."
}

cmd_up() {
  local replicas="${1:-${AGENT_RUNNER_REPLICAS:-3}}"
  [[ "$replicas" =~ ^[0-9]+$ ]] || die "replicas must be a number, got '$replicas'"
  command -v docker >/dev/null || die "docker is not installed on this host"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running (or not accessible)"
  require_env

  # This machine's name in the admin host switch. Resolved here because the
  # script runs on the host: inside a container the hostname is a container id,
  # so a runner cannot answer "which machine am I" for itself.
  if [[ -z "$(env_or_dotenv AGENT_RUNNER_HOST)" ]]; then
    AGENT_RUNNER_HOST="$(hostname -s 2>/dev/null || hostname)"
    export AGENT_RUNNER_HOST
    log "this machine joins the host pool as '${AGENT_RUNNER_HOST}'" \
        "(set AGENT_RUNNER_HOST in .env to rename it)"
  fi

  log "building the sandbox image (${SANDBOX_IMAGE})"
  docker build -f deploy/docker/Dockerfile.agent-sandbox -t "$SANDBOX_IMAGE" .

  log "starting the gateway tunnel and ${replicas} runner(s)"
  AGENT_SANDBOX_IMAGE="$SANDBOX_IMAGE" AGENT_RUNNER_REPLICAS="$replicas" \
    "${COMPOSE[@]}" up -d --build

  # The runner's preflight now makes a real request to the gateway through the
  # tunnel, so a tunnel that came up without a working upstream fails here
  # rather than one job at a time. Give it a moment and surface the verdict.
  sleep 5
  cmd_check || true
  "${COMPOSE[@]}" logs --tail 20 agent-runner
  log "done. 'ops/deploy/agent_remote_runner.sh status' shows the live tail."
}

cmd_check() {
  # Ask from inside the closed network, which is the only vantage point whose
  # answer means anything: reaching the gateway from this host's shell proves
  # nothing about what a sandbox can reach.
  local network="${AGENT_EGRESS_NETWORK_PLATFORM_ONLY:-agent-egress}"
  local url="http://${GATEWAY_HOSTNAME}:${TUNNEL_PORT}/health"
  log "probing ${url} from the ${network} network"
  if docker run --rm --network "$network" --entrypoint /bin/sh "$SANDBOX_IMAGE" \
       -c "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 '$url'" 2>/dev/null; then
    printf '\n'
    log "the sandbox network can reach the gateway through the tunnel"
    return 0
  fi
  printf '\n'
  die "a sandbox on ${network} cannot reach ${url}.
     Check 'docker logs' for the agent-gateway-tunnel container: an ssh that
     cannot authenticate or cannot bind its forward exits, and the container
     restarts. Resolving the name is not enough — the forward has to be up."
}

cmd_status() {
  "${COMPOSE[@]}" ps
  printf '\n'
  "${COMPOSE[@]}" logs --tail 20 agent-gateway-tunnel agent-runner
}

cmd_down() {
  "${COMPOSE[@]}" down
  log "stopped. Jobs that were running were interrupted; they are retried when"
  log "their lease expires."
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  check) cmd_check ;;
  status) cmd_status ;;
  down) cmd_down ;;
  *) die "usage: $0 {up [N]|check|status|down}" ;;
esac
