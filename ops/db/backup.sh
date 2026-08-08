#!/bin/bash
# Database Backup Script for hybridInference
#
# Dumps the PostgreSQL database with pg_dump. With --s3-bucket the dump is
# streamed straight to S3 and never lands on local disk; without it a
# compressed dump is written under --backup-dir.
#
# Usage:
#   ./ops/db/backup.sh [OPTIONS]
#
# Options:
#   --backup-dir PATH     Local backup root (default: ./backups). Ignored when
#                         streaming to S3.
#   --compress            Compress with zstd (default: on)
#   --no-compress         Write an uncompressed .sql dump (local mode only)
#   --s3-bucket URI       Stream the dump to S3 (e.g. s3://your-bucket/prefix)
#   --s3-only             Accepted for backwards compatibility. Streaming never
#                         writes a local dump, so this is now a no-op.
#   --keep-daily N        Keep the N most recent backups (default: 3)
#   --keep-weekly N       Keep N more, one per ISO week (default: 2)
#   --keep-monthly N      Keep N more, one per calendar month (default: 1)
#   --help                Show this help message
#
# Environment variables:
#   DB_NAME, DB_USER, DB_PASSWORD   PostgreSQL credentials (read from .env)
#   BACKUP_ALERT_WEBHOOK_URL        POST a JSON failure report here (optional)
#   BACKUP_EXPECTED_SIZE            --expected-size for the S3 upload
#   BACKUP_MIN_OBJECT_BYTES         Reject an uploaded object smaller than this
#   BACKUP_PROJECT_ROOT             Override the detected project root
#   POSTGRES_CONTAINER              Container to run pg_dump in
#   DOCKER_COMPOSE_FILE             Compose file named in the "not running" hint
#
# Why the dump is streamed:
#   api_logs alone is larger than the free space on the volume Postgres lives
#   on, so a dump written locally and then uploaded cannot fit. The dump is
#   piped through zstd into `aws s3 cp -`, which needs --expected-size: without
#   it the CLI assumes 8MB x 10000 parts and dies at 80GB, hours in.
#
# Why an upload that "succeeded" is still checked:
#   A truncated pipe still produces a valid zstd frame and `aws s3 cp -` still
#   completes the multipart upload, so a half dump lands looking healthy. Every
#   run therefore checks the status of every stage of the pipe, asserts
#   pg_dump's own end-of-dump sentinel is in the last bytes it wrote, and
#   verifies the object's size — uploading to a .partial key and promoting it
#   only once all three pass. Old backups are pruned only after that promotion.
#
# Examples:
#   ./ops/db/backup.sh
#   ./ops/db/backup.sh --compress
#   ./ops/db/backup.sh --backup-dir /mnt/backups
#   ./ops/db/backup.sh --compress --s3-bucket s3://your-bucket/hybridinference/backup
#   ./ops/db/backup.sh --keep-daily 5 --keep-weekly 3 --keep-monthly 2

set -Eeuo pipefail

# Color codes for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m' # No Color

# Default configuration
BACKUP_ROOT="./backups"
BACKUP_DIR=""
COMPRESS=true
S3_BUCKET=""
S3_BUCKET_NAME=""
KEEP_DAILY=3
KEEP_WEEKLY=2
KEEP_MONTHLY=1

DOCKER_COMPOSE_FILE="${DOCKER_COMPOSE_FILE:-deploy/docker/docker-compose.yml}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-hybridinference-postgres}"

# The last line pg_dump writes when it finishes cleanly. Its absence is the
# only reliable signal that a dump was cut short.
readonly DUMP_SENTINEL="-- PostgreSQL database dump complete"
# How many trailing bytes of the dump to keep for that check.
readonly TAIL_BYTES=400

# Generous on purpose: --expected-size only sets the multipart part size, and
# over-estimating costs nothing while under-estimating fails the upload.
EXPECTED_SIZE="${BACKUP_EXPECTED_SIZE:-250000000000}"
# A dump of this database cannot plausibly compress below this.
MIN_OBJECT_BYTES="${BACKUP_MIN_OBJECT_BYTES:-1048576}"
# Local-mode free-space guard; see preflight(). Streaming needs no local space.
SKIP_SPACE_CHECK=false

# Script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${BACKUP_PROJECT_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"

# Run state, read by the failure notifier.
STREAMING=false
STAGE="startup"
FAILURE_DETAIL=""
BACKUP_TARGET=""
TAIL_FILE=""
RUN_TS=""

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

# Abort with a reason the failure notifier can report.
die() {
    FAILURE_DETAIL="$*"
    log_error "$*"
    exit 1
}

# Show help message (prints only the header comment block, stops at first blank line)
show_help() {
    sed -n '2,/^$/{ s/^# \?//; p }' "$0"
    exit 0
}

# ============================================================================
# Failure notification
#
# Four consecutive nights failed without anyone noticing, because nothing was
# watching. cron's MAILTO covers the "script ran and printed to stderr" case;
# this covers the "we want it in chat" case. Unset URL means no-op.
# ============================================================================
json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/ }"
    s="${s//$'\r'/ }"
    s="${s//$'\t'/ }"
    printf '%s' "$s"
}

notify_failure() {
    local reason="$1"
    local url="${BACKUP_ALERT_WEBHOOK_URL:-}"

    if [[ -z "$url" ]]; then
        return 0
    fi
    if ! command -v curl &> /dev/null; then
        log_warning "BACKUP_ALERT_WEBHOOK_URL is set but curl is missing; no alert sent"
        return 0
    fi

    local text payload
    text="hybridInference DB backup FAILED at stage '${STAGE}': ${reason}"
    payload=$(printf '{"service":"hybridinference-db-backup","status":"failed","host":"%s","stage":"%s","target":"%s","reason":"%s","timestamp":"%s","text":"%s"}' \
        "$(json_escape "${HOSTNAME:-unknown}")" \
        "$(json_escape "$STAGE")" \
        "$(json_escape "$BACKUP_TARGET")" \
        "$(json_escape "$reason")" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$(json_escape "$text")")

    # -f matters: without it curl exits 0 on a 4xx/5xx, so a revoked or typo'd
    # webhook URL would be reported as a delivered alert. This is the only live
    # channel (MAILTO needs a working MTA), and the whole point of the change is
    # that four consecutive nightly failures went unnoticed — an alert path that
    # lies about delivering is worse than none.
    local http_code=0
    if http_code=$(curl -fsS -m 15 --retry 2 --retry-delay 2 \
        -X POST -H 'Content-Type: application/json' \
        -d "$payload" "$url" -o /dev/null -w '%{http_code}'); then
        log_info "Failure alert delivered to BACKUP_ALERT_WEBHOOK_URL (HTTP ${http_code})"
    else
        log_error "Failure alert POST to BACKUP_ALERT_WEBHOOK_URL FAILED (HTTP ${http_code:-none}) — this failure is unreported"
    fi
}

# Records where `set -e` is about to abort, so the EXIT handler can say what
# broke instead of just reporting a status code.
on_err() {
    if [[ -z "$FAILURE_DETAIL" ]]; then
        FAILURE_DETAIL="line $1: $2"
    fi
}

on_exit() {
    local code=$?
    if [[ -n "$TAIL_FILE" ]]; then
        rm -f "$TAIL_FILE"
    fi
    if [[ $code -ne 0 ]]; then
        local reason="${FAILURE_DETAIL:-exited with status ${code}}"
        log_error "==================================="
        log_error "Backup FAILED during '${STAGE}': ${reason}"
        log_error "==================================="
        notify_failure "$reason" || true
    fi
    exit "$code"
}

# Parse command line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --keep-daily)
                KEEP_DAILY="$2"
                shift 2
                ;;
            --keep-weekly)
                KEEP_WEEKLY="$2"
                shift 2
                ;;
            --keep-monthly)
                KEEP_MONTHLY="$2"
                shift 2
                ;;
            --backup-dir)
                BACKUP_ROOT="$2"
                shift 2
                ;;
            --compress)
                COMPRESS=true
                shift
                ;;
            --no-compress)
                COMPRESS=false
                shift
                ;;
            --s3-bucket)
                S3_BUCKET="$2"
                shift 2
                ;;
            --s3-only)
                # Kept so the installed cron entry keeps working. Streaming
                # never writes a local dump, so there is nothing to remove.
                shift
                ;;
            --skip-space-check)
                SKIP_SPACE_CHECK=true
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

    local name value
    for name in KEEP_DAILY KEEP_WEEKLY KEEP_MONTHLY; do
        value="${!name}"
        if ! [[ "$value" =~ ^[0-9]+$ ]]; then
            die "${name} must be a non-negative integer (got: ${value})"
        fi
    done
}

# Split s3://bucket/prefix into its parts.
parse_s3_target() {
    S3_BUCKET="${S3_BUCKET%/}"
    if [[ "$S3_BUCKET" != s3://* ]]; then
        die "--s3-bucket must be an s3:// URI (got: ${S3_BUCKET})"
    fi
    local rest="${S3_BUCKET#s3://}"
    S3_BUCKET_NAME="${rest%%/*}"
    if [[ -z "$S3_BUCKET_NAME" ]]; then
        die "--s3-bucket is missing a bucket name (got: ${S3_BUCKET})"
    fi
}

# Load environment variables from .env file
load_env() {
    local env_file="${PROJECT_ROOT}/.env"

    if [[ ! -f "$env_file" ]]; then
        log_warning ".env file not found at ${env_file}"
        log_warning "PostgreSQL backup may fail without credentials"
        return 1
    fi

    # Export variables from .env (ignore comments and empty lines)
    set -a
    # shellcheck disable=SC1090
    source <(grep -v '^#' "$env_file" | grep -v '^$' | sed 's/\r$//')
    set +a

    log_info "Loaded environment variables from .env"
}

# ============================================================================
# Preflight
#
# The dump takes hours. Everything that can be known in advance is checked
# first: the first real-world failure of this script burned a full dump and
# only then discovered the credentials were unresolvable.
# ============================================================================
preflight() {
    STAGE="preflight"
    log_info "Preflight checks..."

    command -v docker &> /dev/null || die "docker not found on PATH"

    # Deliberately not `docker ps | grep -q`: grep exits as soon as it matches,
    # which can SIGPIPE docker and, under pipefail, read as "not running".
    local running
    running=$(docker ps --format '{{.Names}}' 2> /dev/null || true)
    if [[ $'\n'"${running}"$'\n' != *$'\n'"${POSTGRES_CONTAINER}"$'\n'* ]]; then
        die "PostgreSQL container '${POSTGRES_CONTAINER}' is not running (start it with: docker compose -f ${DOCKER_COMPOSE_FILE} up -d)"
    fi
    log_info "  container '${POSTGRES_CONTAINER}': running"

    if [[ -z "${DB_NAME:-}" ]] || [[ -z "${DB_USER:-}" ]] || [[ -z "${DB_PASSWORD:-}" ]]; then
        die "DB_NAME, DB_USER and DB_PASSWORD must be set (looked in ${PROJECT_ROOT}/.env)"
    fi
    log_info "  database credentials: present"

    if [[ "$COMPRESS" == true ]]; then
        command -v zstd &> /dev/null || die "zstd not found on PATH (needed to compress the dump)"
        log_info "  zstd: available"
    fi

    # Local mode writes the whole compressed dump to disk. On this deployment
    # that filesystem also holds the Postgres data directory, so running out of
    # space does not merely fail the backup — it can take the database down.
    # Two of the four nightly failures that motivated this rewrite were exactly
    # `zstd: error 25 : Write error : No space left on device`. Streaming to S3
    # needs no local room, so this only guards the local path.
    if [[ -z "$S3_BUCKET" ]] && [[ "$SKIP_SPACE_CHECK" != true ]]; then
        local probe_dir avail_bytes db_bytes need_bytes
        probe_dir="$BACKUP_ROOT"
        while [[ -n "$probe_dir" && ! -d "$probe_dir" ]]; do
            probe_dir="$(dirname "$probe_dir")"
        done
        avail_bytes=$(df -Pk "${probe_dir:-/}" 2> /dev/null | awk 'NR==2 {print $4 * 1024}')

        db_bytes=$(docker exec -e PGPASSWORD="${DB_PASSWORD}" "${POSTGRES_CONTAINER}" \
            psql -h localhost -U "${DB_USER}" -d "${DB_NAME}" -X -A -t \
            -c "SELECT pg_database_size(current_database());" 2> /dev/null | tr -dc '0-9')

        if [[ -z "$avail_bytes" || -z "$db_bytes" ]]; then
            log_warning "  free space: could not determine (skipping check)"
        else
            # Compression on this data runs ~4x; require 1/3 of the logical size
            # so a worse-than-usual ratio still fits. BACKUP_MIN_FREE_BYTES
            # overrides the estimate outright.
            need_bytes="${BACKUP_MIN_FREE_BYTES:-$((db_bytes / 3))}"
            if [[ "$avail_bytes" -lt "$need_bytes" ]]; then
                die "insufficient free space in ${probe_dir}: $((avail_bytes / 1073741824)) GiB available, ~$((need_bytes / 1073741824)) GiB needed for a local dump of a $((db_bytes / 1073741824)) GiB database. Stream to S3 with --s3-bucket (needs no local space), or override with BACKUP_MIN_FREE_BYTES / --skip-space-check."
            fi
            log_info "  free space: $((avail_bytes / 1073741824)) GiB available, ~$((need_bytes / 1073741824)) GiB needed"
        fi
    fi

    if [[ -n "$S3_BUCKET" ]]; then
        command -v aws &> /dev/null || die "AWS CLI not found (needed to upload to ${S3_BUCKET})"
        if ! aws sts get-caller-identity > /dev/null 2>&1; then
            die "AWS credentials are unusable (aws sts get-caller-identity failed). Under cron the CLI reads the invoking user's ~/.aws."
        fi
        log_info "  aws credentials: usable"
        if ! aws s3api head-bucket --bucket "$S3_BUCKET_NAME" > /dev/null 2>&1; then
            die "S3 bucket '${S3_BUCKET_NAME}' is not reachable — it may not exist, be in another region, or be denied to this identity"
        fi
        log_info "  bucket '${S3_BUCKET_NAME}': reachable"
    fi

    log_success "Preflight passed"
}

# The dump itself. Writes SQL on stdout, progress on stderr.
pg_dump_stream() {
    docker exec -e PGPASSWORD="${DB_PASSWORD}" "${POSTGRES_CONTAINER}" pg_dump \
        -h localhost \
        -U "${DB_USER}" \
        -d "${DB_NAME}" \
        --clean \
        --if-exists \
        --create \
        --verbose
}

# True when the tail of what pg_dump wrote carries its end-of-dump sentinel.
#
# `tee >(...)` is reaped asynchronously, so the tail may not have flushed when
# the pipeline returns; wait for it to appear before concluding anything. `tail
# -c` emits its whole window in one write at EOF, so a non-empty file is a
# complete one.
dump_is_complete() {
    local i
    for ((i = 0; i < 50; i++)); do
        if [[ -s "$TAIL_FILE" ]]; then
            break
        fi
        sleep 0.1
    done
    grep -qF -- "$DUMP_SENTINEL" "$TAIL_FILE" 2> /dev/null
}

# Describe every stage of a pipeline that did not exit 0, and fail if any did.
#
# Usage: describe_pipeline_failures "<stage names>" "${PIPESTATUS[@]}"
#
# Reporting all of them matters: when `aws` dies mid-upload it SIGPIPEs
# everything upstream, so the first non-zero status is pg_dump's 141 and the
# stage that actually broke is the one after it.
describe_pipeline_failures() {
    local names_str="$1"
    shift

    local -a names=()
    read -r -a names <<< "$names_str"

    local -a failed=()
    local index=0 code
    for code in "$@"; do
        if [[ "$code" -ne 0 ]]; then
            failed+=("${names[$index]:-stage${index}} exited ${code}")
        fi
        index=$((index + 1))
    done

    if [[ ${#failed[@]} -eq 0 ]]; then
        return 0
    fi

    local IFS=", "
    printf '%s' "${failed[*]}"
    return 1
}

# Byte size of an S3 object, or empty when it cannot be read back.
s3_object_size() {
    local uri="$1"
    local key="${uri#s3://${S3_BUCKET_NAME}/}"
    aws s3api head-object --bucket "${S3_BUCKET_NAME}" --key "${key}" \
        --query ContentLength --output text 2> /dev/null || true
}

# Best-effort removal of an object that must not be mistaken for a backup.
discard_partial_object() {
    local uri="$1"
    log_warning "Discarding partial object ${uri}"
    aws s3 rm "$uri" --only-show-errors > /dev/null 2>&1 || true
}

# ============================================================================
# Streaming backup: pg_dump -> zstd -> S3, nothing on local disk.
# ============================================================================
run_streaming_backup() {
    STAGE="dump"
    local key="${S3_BUCKET}/${DB_NAME}_${RUN_TS}.sql.zst"
    local partial="${key}.partial"
    BACKUP_TARGET="$key"

    log_info "Streaming dump of '${DB_NAME}' to ${partial}"
    log_info "  (expected-size ${EXPECTED_SIZE} — without it the CLI caps the upload at 80GB)"

    local -a status=()

    set +e
    pg_dump_stream \
        | tee >(tail -c "${TAIL_BYTES}" > "${TAIL_FILE}") \
        | zstd -3 -T4 -c \
        | aws s3 cp - "${partial}" --expected-size "${EXPECTED_SIZE}" --only-show-errors
    status=("${PIPESTATUS[@]}")
    set -e

    STAGE="verify"
    local failures
    if ! failures=$(describe_pipeline_failures "pg_dump tee zstd aws" "${status[@]}"); then
        discard_partial_object "$partial"
        die "dump pipeline failed: ${failures}"
    fi
    log_success "Pipeline finished: every stage exited 0"

    if ! dump_is_complete; then
        discard_partial_object "$partial"
        die "dump is truncated: '${DUMP_SENTINEL}' missing from its last ${TAIL_BYTES} bytes"
    fi
    log_success "Dump reached pg_dump's end-of-dump marker"

    local uploaded_bytes
    uploaded_bytes=$(s3_object_size "$partial")
    if ! [[ "$uploaded_bytes" =~ ^[0-9]+$ ]]; then
        discard_partial_object "$partial"
        die "uploaded object ${partial} could not be read back with head-object"
    fi
    if [[ "$uploaded_bytes" -lt "$MIN_OBJECT_BYTES" ]]; then
        discard_partial_object "$partial"
        die "uploaded object is only ${uploaded_bytes} bytes (minimum ${MIN_OBJECT_BYTES}) — refusing to promote it"
    fi
    log_success "Uploaded object verified: ${uploaded_bytes} bytes"

    STAGE="promote"
    # Server-side copy + delete: no bytes leave S3. Unlike the streamed upload,
    # the CLI knows the object's size here and sizes the multipart copy itself,
    # so this needs no --expected-size equivalent. It still takes minutes at
    # 120GB.
    log_info "Promoting ${partial} -> ${key}"
    if ! aws s3 mv "$partial" "$key" --only-show-errors; then
        die "failed to promote ${partial} to ${key}"
    fi

    local final_bytes
    final_bytes=$(s3_object_size "$key")
    if [[ "$final_bytes" != "$uploaded_bytes" ]]; then
        die "promoted object is ${final_bytes} bytes, expected ${uploaded_bytes}"
    fi

    log_success "Backup uploaded: ${key} (${final_bytes} bytes)"
}

# ============================================================================
# Local backup: still compressed on the fly, still verified before promotion.
# ============================================================================
run_local_backup() {
    STAGE="dump"
    BACKUP_DIR="${BACKUP_ROOT}/backup_${RUN_TS}"
    mkdir -p "$BACKUP_DIR"
    log_info "Created backup directory: ${BACKUP_DIR}"

    local final="${BACKUP_DIR}/${DB_NAME}_${RUN_TS}.sql"
    if [[ "$COMPRESS" == true ]]; then
        final="${final}.zst"
    fi
    local partial="${final}.partial"
    BACKUP_TARGET="$final"

    log_info "Dumping database '${DB_NAME}' to ${final}"

    local -a status=()
    local names=""

    set +e
    if [[ "$COMPRESS" == true ]]; then
        names="pg_dump tee zstd"
        pg_dump_stream \
            | tee >(tail -c "${TAIL_BYTES}" > "${TAIL_FILE}") \
            | zstd -3 -T4 -c > "$partial"
        status=("${PIPESTATUS[@]}")
    else
        names="pg_dump tee"
        pg_dump_stream \
            | tee >(tail -c "${TAIL_BYTES}" > "${TAIL_FILE}") > "$partial"
        status=("${PIPESTATUS[@]}")
    fi
    set -e

    STAGE="verify"
    local failures
    if ! failures=$(describe_pipeline_failures "$names" "${status[@]}"); then
        rm -f "$partial"
        die "dump pipeline failed: ${failures}"
    fi

    if ! dump_is_complete; then
        rm -f "$partial"
        die "dump is truncated: '${DUMP_SENTINEL}' missing from its last ${TAIL_BYTES} bytes"
    fi
    log_success "Dump reached pg_dump's end-of-dump marker"

    if [[ ! -s "$partial" ]]; then
        rm -f "$partial"
        die "dump produced an empty file"
    fi

    STAGE="promote"
    mv "$partial" "$final"

    local size
    size=$(du -h "$final" | cut -f1)
    log_success "PostgreSQL backup completed: ${final} (${size})"

    create_summary
}

# Create backup summary
create_summary() {
    local summary_file="${BACKUP_DIR}/backup_summary.txt"

    {
        echo "==================================="
        echo "hybridInference Database Backup"
        echo "==================================="
        echo ""
        echo "Timestamp: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "Backup Directory: ${BACKUP_DIR}"
        echo "Retention Policy: ${KEEP_DAILY} daily, ${KEEP_WEEKLY} weekly, ${KEEP_MONTHLY} monthly"
        echo "Compression: ${COMPRESS}"
        echo ""
        echo "--- Backup Contents ---"
        echo ""

        find "${BACKUP_DIR}" -type f ! -name "backup_summary.txt" -exec ls -lh {} \; \
            | awk '{print "  " $9 " (" $5 ")"}'

        echo ""
        echo "--- Total Size ---"
        du -sh "${BACKUP_DIR}" | awk '{print $1}'

    } > "$summary_file"

    log_success "Backup summary created: ${summary_file}"
    cat "$summary_file"
}

# ============================================================================
# GFS Retention Policy (Grandfather-Father-Son)
#
# Given YYYYMMDD dates (one per line, newest first) on stdin, print the dates
# to KEEP for --keep-daily / --keep-weekly / --keep-monthly.
#
#   1. Keep the N most recent dates.
#   2. Keep one per ISO week for M more weeks, skipping weeks the dailies
#      already cover.
#   3. Keep one per calendar month for K more months, skipping months the
#      dailies and weeklies already cover.
#
# Steps 2 and 3 previously did not seed themselves with what the earlier steps
# had covered, so 60 consecutive daily dumps collapsed to six *consecutive*
# days — a retention policy that looks like GFS and retains nothing older than
# a week.
# ============================================================================
iso_week() {
    local d="$1"
    date -d "${d:0:4}-${d:4:2}-${d:6:2}" +%G-W%V 2> /dev/null || true
}

compute_keep_set() {
    local dates=()
    local d
    while IFS= read -r d; do
        if [[ -n "$d" ]]; then
            dates+=("$d")
        fi
    done

    if [[ ${#dates[@]} -eq 0 ]]; then
        return 0
    fi

    declare -A keep_set=()
    declare -A seen_weeks=()
    declare -A seen_months=()

    # --- Daily: the KEEP_DAILY most recent dates ---
    local daily_count=0 week
    for d in "${dates[@]}"; do
        if [[ $daily_count -ge $KEEP_DAILY ]]; then
            break
        fi
        keep_set["$d"]=1
        week="$(iso_week "$d")"
        if [[ -n "$week" ]]; then
            seen_weeks["$week"]=1
        fi
        seen_months["${d:0:6}"]=1
        daily_count=$((daily_count + 1))
    done

    # --- Weekly: one per ISO week not already covered ---
    local weekly_count=0
    for d in "${dates[@]}"; do
        if [[ $weekly_count -ge $KEEP_WEEKLY ]]; then
            break
        fi
        if [[ -n "${keep_set[$d]:-}" ]]; then
            continue
        fi
        week="$(iso_week "$d")"
        if [[ -z "$week" ]] || [[ -n "${seen_weeks[$week]:-}" ]]; then
            continue
        fi
        seen_weeks["$week"]=1
        seen_months["${d:0:6}"]=1
        keep_set["$d"]=1
        weekly_count=$((weekly_count + 1))
    done

    # --- Monthly: one per calendar month not already covered ---
    local monthly_count=0 month
    for d in "${dates[@]}"; do
        if [[ $monthly_count -ge $KEEP_MONTHLY ]]; then
            break
        fi
        if [[ -n "${keep_set[$d]:-}" ]]; then
            continue
        fi
        month="${d:0:6}"
        if [[ -n "${seen_months[$month]:-}" ]]; then
            continue
        fi
        seen_months["$month"]=1
        keep_set["$d"]=1
        monthly_count=$((monthly_count + 1))
    done

    for d in "${!keep_set[@]}"; do
        printf '%s\n' "$d"
    done
}

# Extract the YYYYMMDD of a backup name (backup_20260807_040001,
# freeinference_20260807_040001.sql.zst). Prints nothing — and still
# succeeds — when there is no date to find.
#
# This used to be `grep -oP ... | head -1`, which exits 1 on no match: under
# `set -e` that aborted the whole run instead of skipping the file, making the
# `[[ -z ]] && continue` guard below unreachable. PR #547 fixed the same class
# of bug here after retention deleted a just-created backup.
extract_date() {
    local name="$1"
    if [[ "$name" =~ ([0-9]{8})_[0-9]{6} ]]; then
        printf '%s' "${BASH_REMATCH[1]}"
    fi
    return 0
}

# True only for objects this script itself wrote: `<db>_YYYYMMDD_HHMMSS.sql[.gz|.zst]`.
#
# Retention deletes whatever it decides not to keep, so matching on the
# timestamp alone is not safe. The prefix we prune (s3://harvardsys-backup/
# freeinference) is shared with other tooling — api_logs CSV archives and
# ad-hoc exports also carry a YYYYMMDD_HHMMSS stamp — and a date-only filter
# would delete them. `.gz` stays in the list so the pre-zstd dumps already in
# S3 are still recognised rather than silently accumulating forever.
is_backup_object() {
    local name="$1"
    [[ "$name" == "${DB_NAME}_"*  ]] || return 1
    case "$name" in
        *.sql | *.sql.gz | *.sql.zst) return 0 ;;
        *) return 1 ;;
    esac
}

# Clean up S3 backups using GFS retention
cleanup_old_s3_backups() {
    STAGE="retention"
    log_info "Applying GFS retention to S3 (daily=${KEEP_DAILY}, weekly=${KEEP_WEEKLY}, monthly=${KEEP_MONTHLY})..."

    local -A file_by_date=()
    local all_dates=()
    local line filename file_date

    while IFS= read -r line; do
        # `2026-08-07 04:00:01  12345 name` — a PRE (prefix) line has no 4th
        # field and drops out here.
        read -r _ _ _ filename <<< "$line"
        if [[ -z "$filename" ]]; then
            continue
        fi
        # A leftover .partial is not a backup; never let one occupy a slot.
        if [[ "$filename" == *.partial ]]; then
            log_warning "Ignoring leftover partial upload: ${filename}"
            continue
        fi
        # Never prune anything this script did not write.
        if ! is_backup_object "$filename"; then
            log_info "Skipping non-backup S3 object: ${filename}"
            continue
        fi
        file_date="$(extract_date "$filename")"
        if [[ -z "$file_date" ]]; then
            log_info "Skipping unrecognized S3 object: ${filename}"
            continue
        fi

        if [[ -z "${file_by_date[$file_date]:-}" ]]; then
            file_by_date["$file_date"]="$filename"
            all_dates+=("$file_date")
        else
            file_by_date["$file_date"]+=" $filename"
        fi
    done < <(aws s3 ls "${S3_BUCKET}/" 2> /dev/null || true)

    if [[ ${#all_dates[@]} -eq 0 ]]; then
        log_info "No S3 backups found"
        return 0
    fi

    local sorted_dates
    sorted_dates=$(printf '%s\n' "${all_dates[@]}" | sort -rn)

    local -A keep_dates=()
    local d
    while IFS= read -r d; do
        keep_dates["$d"]=1
    done < <(printf '%s\n' "$sorted_dates" | compute_keep_set)

    local deleted_count=0
    for d in "${all_dates[@]}"; do
        if [[ -n "${keep_dates[$d]:-}" ]]; then
            continue
        fi
        for filename in ${file_by_date[$d]}; do
            log_info "Deleting S3 backup: ${filename} (date: ${d})"
            if aws s3 rm "${S3_BUCKET}/${filename}" --only-show-errors; then
                deleted_count=$((deleted_count + 1))
            else
                log_warning "Could not delete ${filename}"
            fi
        done
    done

    if [[ $deleted_count -gt 0 ]]; then
        log_success "Deleted ${deleted_count} old S3 backup(s)"
    else
        log_info "No old S3 backups to delete"
    fi
    log_info "Keeping ${#keep_dates[@]} S3 backup date(s)"
}

# Clean up local backups using GFS retention
cleanup_old_backups() {
    STAGE="retention"

    if [[ ! -d "$BACKUP_ROOT" ]]; then
        log_info "No old backups to clean up"
        return 0
    fi

    log_info "Applying GFS retention to local backups (daily=${KEEP_DAILY}, weekly=${KEEP_WEEKLY}, monthly=${KEEP_MONTHLY})..."

    local -A dir_by_date=()
    local all_dates=()
    local dir dir_date

    while IFS= read -r -d '' dir; do
        dir_date="$(extract_date "$(basename "$dir")")"
        if [[ -z "$dir_date" ]]; then
            log_info "Skipping unrecognized backup directory: ${dir}"
            continue
        fi

        if [[ -z "${dir_by_date[$dir_date]:-}" ]]; then
            dir_by_date["$dir_date"]="$dir"
            all_dates+=("$dir_date")
        else
            dir_by_date["$dir_date"]+=" $dir"
        fi
    done < <(find "$BACKUP_ROOT" -maxdepth 1 -type d -name "backup_*" -print0)

    if [[ ${#all_dates[@]} -eq 0 ]]; then
        log_info "No local backups found"
        return 0
    fi

    local sorted_dates
    sorted_dates=$(printf '%s\n' "${all_dates[@]}" | sort -rn)

    local -A keep_dates=()
    local d
    while IFS= read -r d; do
        keep_dates["$d"]=1
    done < <(printf '%s\n' "$sorted_dates" | compute_keep_set)

    local deleted_count=0
    for d in "${all_dates[@]}"; do
        if [[ -n "${keep_dates[$d]:-}" ]]; then
            continue
        fi
        for dir in ${dir_by_date[$d]}; do
            log_info "Deleting local backup: ${dir} (date: ${d})"
            rm -rf "$dir"
            deleted_count=$((deleted_count + 1))
        done
    done

    if [[ $deleted_count -gt 0 ]]; then
        log_success "Deleted ${deleted_count} old local backup(s)"
    else
        log_info "No old local backups to delete"
    fi
}

# Main execution
main() {
    trap 'on_err "$LINENO" "$BASH_COMMAND"' ERR
    trap on_exit EXIT

    cd "$PROJECT_ROOT"
    parse_args "$@"

    log_info "==================================="
    log_info "hybridInference Database Backup"
    log_info "==================================="
    echo ""

    load_env || true

    if [[ -n "$S3_BUCKET" ]]; then
        parse_s3_target
        STREAMING=true
        if [[ "$COMPRESS" == false ]]; then
            log_info "Compression is required when streaming to S3; enabling it"
            COMPRESS=true
        fi
    fi

    preflight

    RUN_TS="$(date +%Y%m%d_%H%M%S)"
    TAIL_FILE="$(mktemp -t hi-backup-tail.XXXXXX)"

    echo ""
    if [[ "$STREAMING" == true ]]; then
        run_streaming_backup
        echo ""
        # Retention runs only now: pruning before the new backup is verified is
        # how a bad night turns into no backups at all.
        cleanup_old_s3_backups
    else
        run_local_backup
        echo ""
        cleanup_old_backups
    fi

    STAGE="done"
    echo ""
    log_success "==================================="
    log_success "Backup completed successfully!"
    log_success "==================================="
    exit 0
}

# Sourcing the script (for tests) must not run a backup.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
