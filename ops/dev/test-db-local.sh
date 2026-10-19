#!/usr/bin/env bash
# Run the database tests against one long-lived local PostgreSQL.
#
#   ops/dev/test-db-local.sh run [pytest args...]   prepare, then run the tests
#   ops/dev/test-db-local.sh env                    print the TEST_DB_* exports
#   ops/dev/test-db-local.sh up | down | clean
#
# One container per machine, shared by every checkout and worktree; its data
# lives in memory and nothing is fsynced, because a test database is
# disposable. Each checkout gets its own database, named from its path, so
# agents working in different worktrees (often on different schemas) never
# share tables. A database is created and migrated once and then reused:
# a later run only re-reads the catalog. Two runs on the same database (one
# worktree, two agents) take turns rather than truncating each other's tables.
#
# Settings (environment):
#   TEST_DB_LOCAL_CONTAINER  container name   (hybridinference-test-postgres)
#   TEST_DB_LOCAL_PORT       host port        (54329, bound to 127.0.0.1)
#   TEST_DB_LOCAL_IMAGE      image            (postgres:15, as CI)
#   TEST_DB_WORKERS          xdist workers    (min(nproc, 8))
#   TEST_DB_LOCAL_NAME       database name    (derived from the checkout path)
#
# TEST_DB_NAME is deliberately not an input: after `eval "$(... env)"` in one
# worktree it is still set in the shell that moves on to the next one.

set -euo pipefail

CONTAINER="${TEST_DB_LOCAL_CONTAINER:-hybridinference-test-postgres}"
PORT="${TEST_DB_LOCAL_PORT:-54329}"
IMAGE="${TEST_DB_LOCAL_IMAGE:-postgres:15}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
UV_RUN="${UV_RUN:-uv run}"

die() {
  echo "test-db-local: $*" >&2
  exit 1
}

workers() {
  if [ -n "${TEST_DB_WORKERS:-}" ]; then
    echo "${TEST_DB_WORKERS}"
    return
  fi
  local cpus
  cpus="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN)"
  # Past eight workers the run is bound by the shared server, not by pytest.
  echo $((cpus < 8 ? cpus : 8))
}

database_name() {
  if [ -n "${TEST_DB_LOCAL_NAME:-}" ]; then
    echo "${TEST_DB_LOCAL_NAME}"
    return
  fi
  local digest
  digest="$(printf '%s' "${ROOT}" | sha1sum | cut -c1-10)"
  echo "hi_${digest}_test_db"
}

export_env() {
  export TEST_DB_HOST=127.0.0.1
  export TEST_DB_PORT="${PORT}"
  export TEST_DB_USER=postgres
  export TEST_DB_PASSWORD=postgres
  TEST_DB_NAME="$(database_name)"
  export TEST_DB_NAME
  export TEST_DB_REQUIRED=1
}

up() {
  command -v docker >/dev/null || die "docker is not installed"
  # Several agents may start at once: whoever loses the `docker run` race
  # retries until the winner's container shows up (it is not visible at once).
  local state attempt
  for attempt in $(seq 1 20); do
    state="$(docker container inspect --format '{{.State.Status}}' "${CONTAINER}" 2>/dev/null || true)"
    case "${state}" in
      running) break ;;
      "")
        docker run --detach --name "${CONTAINER}" \
          --restart unless-stopped \
          --publish "127.0.0.1:${PORT}:5432" \
          --env POSTGRES_PASSWORD=postgres \
          --tmpfs /var/lib/postgresql/data:rw,size=4g \
          --label org.hybridinference.role=test-database \
          "${IMAGE}" \
          -c fsync=off -c synchronous_commit=off -c full_page_writes=off \
          -c max_connections=500 -c shared_buffers=512MB >/dev/null 2>&1 && break
        ;;
      created | restarting) ;;
      *) docker start "${CONTAINER}" >/dev/null 2>&1 && break ;;
    esac
    sleep 0.5
  done
  [ -n "$(docker container inspect --format '{{.Id}}' "${CONTAINER}" 2>/dev/null)" ] ||
    die "could not start ${CONTAINER} (is port ${PORT} free? see: docker run ... ${IMAGE})"
  # Over TCP: during first start-up the image runs a temporary server on the
  # Unix socket only, which pg_isready would report as ready.
  local _
  for _ in $(seq 1 60); do
    if docker exec "${CONTAINER}" pg_isready -q -h 127.0.0.1 -U postgres 2>/dev/null; then
      return
    fi
    sleep 0.5
  done
  die "${CONTAINER} did not become ready; see: docker logs ${CONTAINER}"
}

# Hold a PostgreSQL advisory lock on this database until the script exits.
# The lock belongs to a psql session reading from this shell (fd 7), so it is
# released whenever the shell goes away, however it goes: no lock file to go
# stale. FIFOs rather than `coproc`, which macOS's bash 3.2 lacks.
lock_database() {
  local dir key got
  dir="$(mktemp -d)"
  mkfifo "${dir}/in" "${dir}/out"
  docker exec -i "${CONTAINER}" psql -U postgres -qAtX <"${dir}/in" >"${dir}/out" &
  exec 7>"${dir}/in" 8<"${dir}/out"
  rm -rf "${dir}"
  key="hashtext('test-db-local:${TEST_DB_NAME}')"
  echo "SELECT pg_try_advisory_lock(${key});" >&7
  read -r got <&8 || die "could not reach ${CONTAINER} to lock ${TEST_DB_NAME}"
  if [ "${got}" != "t" ]; then
    echo "test-db-local: another run is using ${TEST_DB_NAME}; waiting for it to finish" >&2
    echo "SELECT 'locked' FROM pg_advisory_lock(${key}) AS taken;" >&7
    read -r got <&8 || die "lost ${CONTAINER} while waiting for ${TEST_DB_NAME}"
  fi
}

prepare() {
  ${UV_RUN} python -m tests.fixtures.postgres prepare --workers "$(workers)" --owner "${ROOT}"
}

cmd="${1:-run}"
[ $# -gt 0 ] && shift
cd "${ROOT}"

case "${cmd}" in
  run)
    up
    export_env
    lock_database
    prepare
    # Same environment as CI's `test (db)` job.
    export USER_AUTH_ENABLED=1
    export API_KEY_SECRET=integration-test-secret-for-ci-only
    export ERASURE_FENCE_SECRET=test-fence-secret-not-for-production
    # The tier stays selected whatever is added (ARGS="-vv", "--lf", a path):
    # without it, extra arguments would also collect the live external tests.
    # A later -m of the caller's own replaces it.
    set -- -m "dbtest and not external" "$@"
    # Not exec: this shell keeps the lock's session open until pytest is done.
    status=0
    ${UV_RUN} pytest -n "$(workers)" --dist loadfile -q -rs "$@" || status=$?
    exit "${status}"
    ;;
  env)
    # Locked while preparing only: the caller's own pytest runs after we exit.
    up
    export_env
    lock_database
    prepare >&2
    printf 'export TEST_DB_HOST=%s TEST_DB_PORT=%s TEST_DB_USER=%s TEST_DB_PASSWORD=%s TEST_DB_NAME=%s TEST_DB_REQUIRED=1\n' \
      "${TEST_DB_HOST}" "${TEST_DB_PORT}" "${TEST_DB_USER}" "${TEST_DB_PASSWORD}" "${TEST_DB_NAME}"
    ;;
  up)
    up
    echo "${CONTAINER} ready on 127.0.0.1:${PORT}"
    ;;
  down)
    # The data is in memory: this discards every checkout's databases, and
    # fails any run in progress in any of them.
    docker rm --force "${CONTAINER}" >/dev/null 2>&1 || true
    echo "${CONTAINER} removed"
    ;;
  clean)
    up
    export_env
    ${UV_RUN} python -m tests.fixtures.postgres clean
    ;;
  *)
    die "unknown command '${cmd}' (run, env, up, down, clean)"
    ;;
esac
