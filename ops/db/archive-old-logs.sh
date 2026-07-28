#!/bin/bash
# api_logs Archive & Prune Script for hybridInference
#
# Exports api_logs rows older than a retention window to a compressed CSV,
# uploads the archive to S3, verifies it, and only then deletes those rows
# from the database.
#
# Safety model (deletion never runs unless all of these hold):
#   1. The archive file is written and passes a zstd integrity check.
#   2. The archive's logical CSV row count matches the DB row count.
#   3. If an S3 target is set, the upload succeeds and the remote object size
#      matches the local file byte-for-byte.
#   4. The DELETE runs in a transaction guarded by GET DIAGNOSTICS: if the
#      number of deleted rows != the archived count, it raises and rolls back.
# A single pinned cutoff timestamp is used for the export, the count check,
# and the delete, so all three operate on the identical set of rows.
#
# NOTE: api_logs contains user prompts/responses (PII). The S3 target you pass
# must be a location you are authorized to store that data in.
#
# NOTE: DELETE marks space reusable inside the table; it does NOT return disk to
# the OS. This script does not run VACUUM FULL (it would rewrite the whole table
# under a lock). Autovacuum reclaims the freed space into the free space map.
#
# Usage:
#   ./ops/db/archive-old-logs.sh [OPTIONS]
#
# Options:
#   --retention-days N    Archive+delete rows older than N days (default: 180)
#   --s3-archive URI      S3 prefix for archives, e.g.
#                         s3://your-bucket/hybridinference/archive/api_logs
#                         When set, the archive is uploaded and size-verified
#                         before any delete.
#   --local-only          Do not upload to S3; keep the archive on local disk.
#                         Required to permit deletion when --s3-archive is unset.
#   --backup-dir PATH     Local dir for the archive file (default: ./backups/archive)
#   --keep-local          Keep the local archive after a successful S3 upload
#   --table NAME          Table to prune (default: api_logs)
#   --ts-column NAME      Timestamp column to compare (default: timestamp)
#   --dry-run             Report counts/size only; write nothing, delete nothing
#   --help                Show this help message
#
# Environment Variables (from .env):
#   DB_NAME, DB_USER, DB_PASSWORD - PostgreSQL credentials
#
# Examples:
#   ./ops/db/archive-old-logs.sh --dry-run
#   ./ops/db/archive-old-logs.sh --local-only
#   ./ops/db/archive-old-logs.sh --s3-archive s3://your-bucket/hybridinference/archive/api_logs
#   ./ops/db/archive-old-logs.sh --retention-days 90 --s3-archive s3://bucket/prefix

set -euo pipefail

# Color codes for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m' # No Color

# Default configuration
RETENTION_DAYS=180
S3_ARCHIVE=""
LOCAL_ONLY=false
BACKUP_DIR="./backups/archive"
KEEP_LOCAL=false
TABLE="api_logs"
TS_COLUMN="timestamp"
DRY_RUN=false
DOCKER_COMPOSE_FILE="deploy/docker/docker-compose.yml"
POSTGRES_CONTAINER="hybridinference-postgres"

# Script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}

# Show help message (prints only the header comment block, stops at first blank line)
show_help() {
    sed -n '2,/^$/{ s/^# \?//; p }' "$0"
    exit 0
}

# Parse command line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --retention-days)
                RETENTION_DAYS="$2"
                shift 2
                ;;
            --s3-archive)
                S3_ARCHIVE="$2"
                shift 2
                ;;
            --local-only)
                LOCAL_ONLY=true
                shift
                ;;
            --backup-dir)
                BACKUP_DIR="$2"
                shift 2
                ;;
            --keep-local)
                KEEP_LOCAL=true
                shift
                ;;
            --table)
                TABLE="$2"
                shift 2
                ;;
            --ts-column)
                TS_COLUMN="$2"
                shift 2
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --help)
                show_help
                ;;
            *)
                log_error "Unknown option: $1"
                echo "Use --help for usage information"
                exit 1
                ;;
        esac
    done
}

# Validate arguments
validate_args() {
    if ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || [[ "$RETENTION_DAYS" -eq 0 ]]; then
        log_error "--retention-days must be a positive integer (got: ${RETENTION_DAYS})"
        exit 1
    fi

    # Guard against SQL identifier injection: table/column are interpolated into SQL.
    if ! [[ "$TABLE" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        log_error "--table must be a valid SQL identifier (got: ${TABLE})"
        exit 1
    fi
    if ! [[ "$TS_COLUMN" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        log_error "--ts-column must be a valid SQL identifier (got: ${TS_COLUMN})"
        exit 1
    fi

    # Deletion requires a durable archive location. Refuse to delete when the
    # only copy would be a local file unless the operator opts in explicitly.
    if [[ "$DRY_RUN" == false && -z "$S3_ARCHIVE" && "$LOCAL_ONLY" == false ]]; then
        log_error "No S3 target set. Pass --s3-archive URI, or --local-only to keep"
        log_error "the archive on local disk only, or --dry-run to preview."
        exit 1
    fi
}

# Load environment variables from .env file
load_env() {
    local env_file="${PROJECT_ROOT}/.env"

    if [[ ! -f "$env_file" ]]; then
        log_warning ".env file not found at ${env_file}"
        log_warning "Database access may fail without credentials"
        return 1
    fi

    set -a
    # shellcheck disable=SC1090
    source <(grep -v '^#' "$env_file" | grep -v '^$' | sed 's/\r$//')
    set +a

    log_info "Loaded environment variables from .env"
}

# Check if PostgreSQL container is running
check_postgres_container() {
    if ! docker ps --format '{{.Names}}' | grep -q "^${POSTGRES_CONTAINER}$"; then
        log_error "PostgreSQL container '${POSTGRES_CONTAINER}' is not running"
        log_info "Start it with: docker compose -f ${DOCKER_COMPOSE_FILE} up -d"
        return 1
    fi
    return 0
}

# Run a query via psql in the postgres container, returning a single scalar.
psql_scalar() {
    docker exec -i -e PGPASSWORD="${DB_PASSWORD}" "${POSTGRES_CONTAINER}" psql \
        -h localhost -U "${DB_USER}" -d "${DB_NAME}" -X -At -c "$1"
}

# Globals set during the run
CUTOFF=""
OLD_ROWS=0
ARCHIVE_FILE=""

# Determine the pinned cutoff and how many rows are older than it.
measure() {
    # Pin an exact cutoff so export, count, and delete all use the identical set.
    CUTOFF=$(psql_scalar "SELECT (now() - interval '${RETENTION_DAYS} days')::timestamptz")

    OLD_ROWS=$(psql_scalar \
        "SELECT count(*) FROM ${TABLE} WHERE ${TS_COLUMN} < '${CUTOFF}'::timestamptz")

    local total_size
    total_size=$(psql_scalar "SELECT pg_size_pretty(pg_total_relation_size('${TABLE}'))")

    log_info "Table:            ${TABLE} (total size ${total_size})"
    log_info "Retention:        ${RETENTION_DAYS} days"
    log_info "Cutoff:           ${CUTOFF}"
    log_info "Rows to archive:  ${OLD_ROWS}"
}

# Export old rows to a compressed CSV (on the fly, never uncompressed on disk).
export_archive() {
    mkdir -p "${BACKUP_DIR}"
    local cutoff_date run_ts
    cutoff_date=$(date -d "${CUTOFF}" +%Y%m%d)
    run_ts=$(date +%Y%m%d_%H%M%S)
    ARCHIVE_FILE="${BACKUP_DIR}/${TABLE}_before_${cutoff_date}_archived_${run_ts}.csv.zst"

    if ! command -v zstd &> /dev/null; then
        log_error "zstd command not found. Please install zstd."
        return 1
    fi

    log_info "Exporting to ${ARCHIVE_FILE} ..."
    # pipefail (set at top) makes a pg failure abort the pipeline.
    docker exec -i -e PGPASSWORD="${DB_PASSWORD}" "${POSTGRES_CONTAINER}" psql \
        -h localhost -U "${DB_USER}" -d "${DB_NAME}" -X -q -v ON_ERROR_STOP=1 -c \
        "COPY (SELECT * FROM ${TABLE} WHERE ${TS_COLUMN} < '${CUTOFF}'::timestamptz ORDER BY 1) TO STDOUT WITH (FORMAT csv, HEADER true)" \
        | zstd -q -o "${ARCHIVE_FILE}"

    local size
    size=$(du -h "${ARCHIVE_FILE}" | cut -f1)
    log_success "Archive written: ${ARCHIVE_FILE} (${size})"
}

# Verify the archive: zstd integrity + logical CSV row count == DB count.
verify_archive() {
    log_info "Verifying archive integrity..."
    if ! zstd -t "${ARCHIVE_FILE}" 2> /dev/null; then
        log_error "Archive failed zstd integrity check: ${ARCHIVE_FILE}"
        return 1
    fi

    if ! command -v python3 &> /dev/null; then
        log_error "python3 not found — needed to verify CSV row count"
        return 1
    fi

    # Count logical CSV rows (handles quoted fields with embedded newlines).
    local archive_rows
    archive_rows=$(zstd -dc "${ARCHIVE_FILE}" | python3 -c '
import csv, sys
csv.field_size_limit(1 << 30)
r = csv.reader(sys.stdin)
next(r, None)  # header
print(sum(1 for _ in r))
')

    if [[ "$archive_rows" != "$OLD_ROWS" ]]; then
        log_error "Row count mismatch: archive has ${archive_rows}, DB has ${OLD_ROWS}"
        log_error "Refusing to delete."
        return 1
    fi

    log_success "Verified: ${archive_rows} rows, integrity OK"
}

# Upload the archive to S3 and verify the remote size matches the local file.
upload_archive() {
    if [[ -z "$S3_ARCHIVE" ]]; then
        return 0
    fi

    if ! command -v aws &> /dev/null; then
        log_error "AWS CLI not found — cannot upload archive to S3"
        return 1
    fi

    local base s3_uri local_bytes
    base=$(basename "${ARCHIVE_FILE}")
    s3_uri="${S3_ARCHIVE%/}/${base}"
    local_bytes=$(stat -c %s "${ARCHIVE_FILE}")

    log_info "Uploading archive to ${s3_uri} ..."
    if ! aws s3 cp "${ARCHIVE_FILE}" "${s3_uri}" --only-show-errors; then
        log_error "S3 upload failed"
        return 1
    fi

    # Verify remote object size matches the local file.
    local bucket key remote_bytes
    bucket=$(echo "${s3_uri}" | sed -E 's#^s3://([^/]+)/.*#\1#')
    key=$(echo "${s3_uri}" | sed -E 's#^s3://[^/]+/##')
    remote_bytes=$(aws s3api head-object --bucket "${bucket}" --key "${key}" \
        --query ContentLength --output text 2> /dev/null || echo "")

    if [[ "$remote_bytes" != "$local_bytes" ]]; then
        log_error "Upload size mismatch: local=${local_bytes} remote=${remote_bytes}"
        log_error "Refusing to delete."
        return 1
    fi

    log_success "Uploaded and verified: ${s3_uri} (${local_bytes} bytes)"
}

# Delete the archived rows, guarded so it rolls back on any count mismatch.
delete_rows() {
    log_info "Deleting ${OLD_ROWS} archived row(s) from ${TABLE} ..."
    docker exec -i -e PGPASSWORD="${DB_PASSWORD}" "${POSTGRES_CONTAINER}" psql \
        -h localhost -U "${DB_USER}" -d "${DB_NAME}" -X -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$
DECLARE n int;
BEGIN
  DELETE FROM ${TABLE} WHERE ${TS_COLUMN} < '${CUTOFF}'::timestamptz;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> ${OLD_ROWS} THEN
    RAISE EXCEPTION 'Guard: expected ${OLD_ROWS} rows, deleted % — rolling back', n;
  END IF;
  RAISE NOTICE 'Deleted % rows', n;
END
\$\$;
SQL
    log_success "Deleted ${OLD_ROWS} row(s) from ${TABLE}"
}

# Remove local archive unless the operator asked to keep it.
cleanup_local() {
    if [[ -z "$S3_ARCHIVE" ]]; then
        log_info "Archive kept locally: ${ARCHIVE_FILE}"
        return 0
    fi
    if [[ "$KEEP_LOCAL" == true ]]; then
        log_info "Keeping local archive (--keep-local): ${ARCHIVE_FILE}"
        return 0
    fi
    rm -f "${ARCHIVE_FILE}"
    log_info "Removed local archive (S3 copy retained)"
}

# Main execution
main() {
    cd "$PROJECT_ROOT"

    parse_args "$@"
    validate_args

    log_info "==================================="
    log_info "hybridInference api_logs Archive & Prune"
    log_info "==================================="
    echo ""

    load_env || true

    if ! check_postgres_container; then
        exit 1
    fi

    if [[ -z "${DB_NAME:-}" ]] || [[ -z "${DB_USER:-}" ]] || [[ -z "${DB_PASSWORD:-}" ]]; then
        log_error "DB_NAME, DB_USER, and DB_PASSWORD must be set in .env file"
        exit 1
    fi

    measure

    if [[ "$OLD_ROWS" -eq 0 ]]; then
        log_success "No rows older than ${RETENTION_DAYS} days — nothing to do."
        exit 0
    fi

    if [[ "$DRY_RUN" == true ]]; then
        echo ""
        log_warning "DRY RUN — would archive and delete ${OLD_ROWS} row(s); no changes made."
        exit 0
    fi

    echo ""
    export_archive
    verify_archive
    upload_archive

    echo ""
    delete_rows
    cleanup_local

    echo ""
    log_success "==================================="
    log_success "Archived and pruned ${OLD_ROWS} row(s) older than ${RETENTION_DAYS} days"
    log_success "==================================="
    exit 0
}

main "$@"
