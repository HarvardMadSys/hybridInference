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
#   ops/deploy/agent_runner.sh preflight  # check this host's isolation, start nothing
#   ops/deploy/agent_runner.sh status     # what is running, and the recent log tail
#   ops/deploy/agent_runner.sh down       # stop the runners (main stack untouched)
#
# `up` gates on the sandbox's isolation boundary before it starts anything: with
# the default kata backend the host must have the Kata shim installed, and one
# real container has to come back reporting a kernel that is not the host's.
# There is no automatic fallback to a shared kernel — accepting one is an
# explicit AGENT_SANDBOX_BACKEND=container in .env, and it says so on every run.
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

# Must match KATA_RUNTIME in apps/backend/serving/agent_jobs/sandbox.py.
KATA_RUNTIME="io.containerd.kata.v2"
KATA_SETUP_SCRIPT="${KATA_SETUP_SCRIPT:-ops/setup/setup_kata_runtime.sh}"

log() { printf '[agent-runner] %s\n' "$*"; }
warn() { printf '[agent-runner] WARNING: %s\n' "$*" >&2; }
die() { printf '[agent-runner] ERROR: %s\n' "$*" >&2; exit 1; }

env_value() {
  # The effective value of a compose variable: the process environment first,
  # then .env, then the default — the precedence compose itself applies. Read
  # here so preflight gates on the same value the runner will actually get.
  local name="$1" fallback="${2:-}" from_file
  if [[ -n "${!name:-}" ]]; then
    printf '%s' "${!name}"
    return 0
  fi
  from_file="$(sed -n "s/^${name}=//p" .env 2>/dev/null | tail -1)"
  from_file="${from_file%\"}"
  from_file="${from_file#\"}"
  from_file="${from_file%\'}"
  from_file="${from_file#\'}"
  printf '%s' "${from_file:-$fallback}"
}

require_kata_host() {
  # Gate before anything starts. The runner's own preflight would fail per job
  # otherwise, which reads as "the queue is broken" rather than "this host was
  # never provisioned" — and that misreading is precisely how staging ended up
  # running every job on a shared kernel for months.
  local backend
  backend="$(env_value AGENT_SANDBOX_BACKEND kata)"

  if [[ "$backend" != "kata" ]]; then
    # Not silently tolerated: this is the deployment saying out loud that it
    # has no kernel boundary, so nobody reads `AGENT_SANDBOX_BACKEND=container`
    # in an .env six months from now and assumes it was reviewed.
    warn "AGENT_SANDBOX_BACKEND=${backend}: sandboxes on this host share the host kernel."
    warn "Repository code runs against the host's own syscall surface. Acceptable for"
    warn "trusted repositories only. For untrusted or multi-tenant work, install Kata"
    warn "(sudo ${KATA_SETUP_SCRIPT}) and drop the override."
    return 0
  fi

  [[ -x "$KATA_SETUP_SCRIPT" ]] || die "missing ${KATA_SETUP_SCRIPT}; cannot verify this host's Kata runtime"
  if ! "$KATA_SETUP_SCRIPT" --check; then
    die "this host is not provisioned for Kata. Install it with:
    sudo ${KATA_SETUP_SCRIPT}
  Or accept a shared host kernel *explicitly* by setting AGENT_SANDBOX_BACKEND=container
  in .env. There is deliberately no automatic fallback: silently downgrading the
  isolation boundary is the one failure mode nobody would notice."
  fi
}

kata_smoke_test() {
  # The authoritative check, and the one nothing else can stand in for: start a
  # real container under the real runtime and ask which kernel it is running.
  # Under runc that is the host's own; under Kata it is the guest's. Every
  # cheaper signal (the flag is accepted, the shim exists) has already passed on
  # hosts where jobs were still sharing the host kernel.
  local backend host_kernel guest_kernel
  backend="$(env_value AGENT_SANDBOX_BACKEND kata)"
  [[ "$backend" == "kata" ]] || return 0

  host_kernel="$(uname -r)"
  log "starting one container under ${KATA_RUNTIME} to confirm it gets its own kernel"
  if ! guest_kernel="$(docker run --rm --runtime "$KATA_RUNTIME" --entrypoint /bin/sh \
    "$SANDBOX_IMAGE" -c 'uname -r' 2>&1)"; then
    die "could not start a container under ${KATA_RUNTIME}: ${guest_kernel}
  The shim is installed but the runtime does not work. '${KATA_SETUP_SCRIPT} --check'
  and 'kata-runtime check' will say more."
  fi
  guest_kernel="$(printf '%s' "$guest_kernel" | tail -1 | tr -d '[:space:]')"

  if [[ "$guest_kernel" == "$host_kernel" ]]; then
    die "the sandbox reported the host's own kernel (${host_kernel}), so it is NOT
  VM-isolated even though the runtime was accepted. Refusing to start runners:
  a deployment that believes it has a kernel boundary and does not is worse than
  one that knows it does not."
  fi
  log "VM isolation confirmed: guest kernel ${guest_kernel}, host kernel ${host_kernel}"
}

cmd_preflight() {
  command -v docker >/dev/null || die "docker is not installed on this host"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running (or not accessible)"
  require_kata_host
  kata_smoke_test
}

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
  # Before the build, because it is the cheap half and a host with no Kata shim
  # should not spend several minutes on an image first.
  require_kata_host

  log "building the sandbox image (${SANDBOX_IMAGE})"
  docker build -f deploy/docker/Dockerfile.agent-sandbox -t "$SANDBOX_IMAGE" .

  # After it, because proving VM isolation means starting a real container from
  # this exact image under the runtime jobs will use.
  kata_smoke_test

  # Which machine this is, for the admin host switch. Resolved here because
  # this script runs on the host: inside the runner container the hostname is
  # a container id, so the name has to be handed in from out here.
  if [[ -z "${AGENT_RUNNER_HOST:-}" ]] && ! grep -q '^AGENT_RUNNER_HOST=..*' .env 2>/dev/null; then
    AGENT_RUNNER_HOST="$(hostname -s 2>/dev/null || hostname)"
    export AGENT_RUNNER_HOST
    log "this machine joins the host pool as '${AGENT_RUNNER_HOST}'" \
        "(set AGENT_RUNNER_HOST in .env to rename it)"
  fi

  log "starting ${replicas} runner(s); restart policy keeps them up across reboots"
  log "persist it with AGENT_RUNNER_REPLICAS=${replicas} in .env, or the next deploy uses the default"
  AGENT_SANDBOX_IMAGE="$SANDBOX_IMAGE" AGENT_RUNNER_REPLICAS="$replicas" \
    "${COMPOSE[@]}" up -d --build agent-runner

  # The runner's own preflight (backend reachable, image spawnable, workdir
  # bind-mountable, egress network resolvable) runs before it claims anything;
  # surface its verdict here instead of leaving it in a detached log.
  sleep 3
  "${COMPOSE[@]}" logs --tail 20 agent-runner
  log "done. 'ops/deploy/agent_runner.sh status' shows the live tail."
}

cmd_status() {
  "${COMPOSE[@]}" ps agent-runner
  "${COMPOSE[@]}" logs --tail 40 agent-runner
}

cmd_down() {
  "${COMPOSE[@]}" rm --stop --force agent-runner
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  preflight) cmd_preflight ;;
  status) cmd_status ;;
  down) cmd_down ;;
  *) die "usage: $0 up [replicas] | preflight | status | down" ;;
esac
