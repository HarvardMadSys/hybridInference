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
# a later run only re-reads the catalog.
#
# Settings (environment):
#   TEST_DB_LOCAL_CONTAINER  container name   (hybridinference-test-postgres)
#   TEST_DB_LOCAL_PORT       host port        (54329, bound to 127.0.0.1)
#   TEST_DB_LOCAL_IMAGE      image            (postgres:15, as CI)
#   TEST_DB_WORKERS          xdist workers    (min(nproc, 8))
#   TEST_DB_NAME             database name    (derived from the checkout path)

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
  if [ -n "${TEST_DB_NAME:-}" ]; then
    echo "${TEST_DB_NAME}"
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
  local state
  state="$(docker container inspect --format '{{.State.Status}}' "${CONTAINER}" 2>/dev/null || true)"
  case "${state}" in
    running) ;;
    "")
      docker run --detach --name "${CONTAINER}" \
        --restart unless-stopped \
        --publish "127.0.0.1:${PORT}:5432" \
        --env POSTGRES_PASSWORD=postgres \
        --tmpfs /var/lib/postgresql/data:rw,size=4g \
        --label org.hybridinference.role=test-database \
        "${IMAGE}" \
        -c fsync=off -c synchronous_commit=off -c full_page_writes=off \
        -c max_connections=500 -c shared_buffers=512MB >/dev/null
      ;;
    *) docker start "${CONTAINER}" >/dev/null ;;
  esac
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
    prepare
    # Same environment as CI's `test (db)` job.
    export USER_AUTH_ENABLED=1
    export API_KEY_SECRET=integration-test-secret-for-ci-only
    export ERASURE_FENCE_SECRET=test-fence-secret-not-for-production
    if [ $# -eq 0 ]; then
      set -- -m "dbtest and not external"
    fi
    exec ${UV_RUN} pytest -n "$(workers)" --dist loadfile -q -rs "$@"
    ;;
  env)
    up
    export_env
    prepare >&2
    printf 'export TEST_DB_HOST=%s TEST_DB_PORT=%s TEST_DB_USER=%s TEST_DB_PASSWORD=%s TEST_DB_NAME=%s TEST_DB_REQUIRED=1\n' \
      "${TEST_DB_HOST}" "${TEST_DB_PORT}" "${TEST_DB_USER}" "${TEST_DB_PASSWORD}" "${TEST_DB_NAME}"
    ;;
  up)
    up
    echo "${CONTAINER} ready on 127.0.0.1:${PORT}"
    ;;
  down)
    # The data is in memory: stopping the container discards every database.
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
