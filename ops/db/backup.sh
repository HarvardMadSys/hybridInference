#!/bin/bash
# Database Backup Script for hybridInference
#
# This script backs up the PostgreSQL database and optionally uploads to S3.
#
# Usage:
#   ./ops/db/backup.sh [OPTIONS]
#
# Options:
#   --backup-dir PATH     Custom backup directory (default: ./backups)
#   --compress            Compress backups with zstd (default: on)
#   --no-compress         Write an uncompressed .sql dump instead
#   --s3-bucket URI       Upload backup to S3 (e.g. s3://your-bucket/hybridinference/backup)
#   --s3-only             Upload to S3 and remove local backup after success
#   --keep-daily N        Keep N most recent daily backups (default: 3)
#   --keep-weekly N       Keep N most recent weekly backups (default: 2)
#   --keep-monthly N      Keep N most recent monthly backups (default: 1)
#   --help                Show this help message
#
# Environment Variables (from .env):
#   DB_NAME, DB_USER, DB_PASSWORD - PostgreSQL credentials
#
# S3 Upload:
#   Requires AWS CLI v2 configured with credentials.
#   Credentials are read from ~/.aws/credentials (or env vars).
#   Credentials come from the AWS CLI's usual sources; under cron that
#   means the invoking user's ~/.aws/, so schedule it as a user that has
#   them.
#
# Examples:
#   ./ops/db/backup.sh
#   ./ops/db/backup.sh --compress
#   ./ops/db/backup.sh --backup-dir /mnt/backups
#   ./ops/db/backup.sh --compress --s3-bucket s3://your-bucket/hybridinference/backup
#   ./ops/db/backup.sh --compress --s3-bucket s3://your-bucket/hybridinference/backup --s3-only
#   ./ops/db/backup.sh --keep-daily 5 --keep-weekly 3 --keep-monthly 2

set -euo pipefail

# Color codes for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m' # No Color

# Default configuration
BACKUP_DIR="./backups"
COMPRESS=true
S3_BUCKET=""
S3_ONLY=false
KEEP_DAILY=3
KEEP_WEEKLY=2
KEEP_MONTHLY=1
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
                BACKUP_DIR="$2"
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
                S3_ONLY=true
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

# Check if PostgreSQL container is running
check_postgres_container() {
    if ! docker ps --format '{{.Names}}' | grep -q "^${POSTGRES_CONTAINER}$"; then
        log_error "PostgreSQL container '${POSTGRES_CONTAINER}' is not running"
        log_info "Start it with: docker compose -f ${DOCKER_COMPOSE_FILE} up -d"
        return 1
    fi
    log_info "PostgreSQL container is running"
    return 0
}

# Create backup directory structure
setup_backup_dir() {
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)

    BACKUP_DIR="${BACKUP_DIR}/backup_${timestamp}"

    mkdir -p "${BACKUP_DIR}"

    log_info "Created backup directory: ${BACKUP_DIR}"
}

# Backup PostgreSQL database
backup_postgres() {
    log_info "Starting PostgreSQL backup..."

    if ! check_postgres_container; then
        return 1
    fi

    # Validate required environment variables
    if [[ -z "${DB_NAME:-}" ]] || [[ -z "${DB_USER:-}" ]] || [[ -z "${DB_PASSWORD:-}" ]]; then
        log_error "DB_NAME, DB_USER, and DB_PASSWORD must be set in .env file"
        return 1
    fi

    local backup_file="${BACKUP_DIR}/${DB_NAME}_$(date +%Y%m%d_%H%M%S).sql"

    # Use pg_dump via docker exec with password and host
    log_info "Dumping database '${DB_NAME}'..."

    # pipefail (set at the top of the script) ensures a pg_dump failure
    # propagates through the zstd pipe below.
    local dump_ok=false
    if [[ "$COMPRESS" == true ]]; then
        # Compress on the fly: pipe pg_dump straight into zstd so the full
        # uncompressed dump never touches disk.
        backup_file="${backup_file}.zst"
        log_info "Compressing on the fly with zstd..."
        if docker exec -e PGPASSWORD="${DB_PASSWORD}" "${POSTGRES_CONTAINER}" pg_dump \
            -h localhost \
            -U "${DB_USER}" \
            -d "${DB_NAME}" \
            --clean \
            --if-exists \
            --create \
            --verbose \
            | zstd -q -o "${backup_file}"; then
            dump_ok=true
        fi
    else
        if docker exec -e PGPASSWORD="${DB_PASSWORD}" "${POSTGRES_CONTAINER}" pg_dump \
            -h localhost \
            -U "${DB_USER}" \
            -d "${DB_NAME}" \
            --clean \
            --if-exists \
            --create \
            --verbose \
            > "${backup_file}"; then
            dump_ok=true
        fi
    fi

    if [[ "$dump_ok" == true ]]; then
        local size
        size=$(du -h "${backup_file}" | cut -f1)
        log_success "PostgreSQL backup completed: ${backup_file} (${size})"
        return 0
    else
        # Remove any partial output so it can't be uploaded or restored.
        rm -f "${backup_file}"
        log_error "PostgreSQL backup failed"
        return 1
    fi
}

# Upload backup to S3
upload_to_s3() {
    if [[ -z "$S3_BUCKET" ]]; then
        return 0
    fi

    # Strip trailing slash from bucket URI
    S3_BUCKET="${S3_BUCKET%/}"

    log_info "Uploading backup to S3: ${S3_BUCKET}/ ..."

    # Check AWS CLI is available
    if ! command -v aws &> /dev/null; then
        log_error "AWS CLI not found. Install it: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
        return 1
    fi

    # Upload all backup files (excluding summary)
    local upload_count=0
    local upload_failed=false

    while IFS= read -r -d '' file; do
        local basename
        basename=$(basename "$file")
        local s3_key="${S3_BUCKET}/${basename}"

        log_info "Uploading ${basename} ..."
        if aws s3 cp "$file" "$s3_key" --quiet; then
            log_success "Uploaded: ${s3_key}"
            upload_count=$((upload_count + 1))
        else
            log_error "Failed to upload: ${basename}"
            upload_failed=true
        fi
    done < <(find "${BACKUP_DIR}" -type f \( -name "*.sql" -o -name "*.sql.zst" \) -print0)

    if [[ "$upload_failed" == true ]]; then
        log_error "Some uploads failed"
        return 1
    fi

    if [[ $upload_count -eq 0 ]]; then
        log_warning "No backup files found to upload"
        return 1
    fi

    log_success "Uploaded ${upload_count} file(s) to S3"

    # Remove local backup if --s3-only
    if [[ "$S3_ONLY" == true ]]; then
        log_info "Removing local backup (--s3-only)..."
        rm -rf "${BACKUP_DIR}"
        log_success "Local backup removed"
    fi

    return 0
}

# ============================================================================
# GFS Retention Policy (Grandfather-Father-Son)
#
# Given a list of YYYYMMDD dates (one per line, newest first), output the
# dates that should be KEPT according to the --keep-daily / --keep-weekly /
# --keep-monthly settings.
#
# Algorithm:
#   1. Keep the N most recent dates as "daily"
#   2. From remaining dates, keep one per ISO week (most recent in that week),
#      up to M "weekly" slots
#   3. From remaining dates, keep one per month (most recent in that month),
#      up to K "monthly" slots
# ============================================================================
compute_keep_set() {
    # Reads sorted dates (newest first) from stdin, prints dates to keep
    local dates=()
    while IFS= read -r d; do
        [[ -n "$d" ]] && dates+=("$d")
    done

    if [[ ${#dates[@]} -eq 0 ]]; then
        return
    fi

    declare -A keep_set=()

    # --- Daily: keep the first KEEP_DAILY entries ---
    local daily_count=0
    for d in "${dates[@]}"; do
        if [[ $daily_count -lt $KEEP_DAILY ]]; then
            keep_set["$d"]=1
            daily_count=$((daily_count + 1))
        fi
    done

    # --- Weekly: one per ISO week, up to KEEP_WEEKLY ---
    local weekly_count=0
    declare -A seen_weeks=()
    for d in "${dates[@]}"; do
        [[ -n "${keep_set[$d]:-}" ]] && continue
        # Compute ISO year-week from YYYYMMDD
        local iso_week
        iso_week=$(date -d "${d:0:4}-${d:4:2}-${d:6:2}" +%G-W%V 2>/dev/null) || continue
        if [[ -z "${seen_weeks[$iso_week]:-}" ]]; then
            seen_weeks["$iso_week"]=1
            keep_set["$d"]=1
            weekly_count=$((weekly_count + 1))
            [[ $weekly_count -ge $KEEP_WEEKLY ]] && break
        fi
    done

    # --- Monthly: one per month, up to KEEP_MONTHLY ---
    local monthly_count=0
    declare -A seen_months=()
    for d in "${dates[@]}"; do
        [[ -n "${keep_set[$d]:-}" ]] && continue
        local month="${d:0:6}"  # YYYYMM
        if [[ -z "${seen_months[$month]:-}" ]]; then
            seen_months["$month"]=1
            keep_set["$d"]=1
            monthly_count=$((monthly_count + 1))
            [[ $monthly_count -ge $KEEP_MONTHLY ]] && break
        fi
    done

    # Output keep set
    for d in "${!keep_set[@]}"; do
        echo "$d"
    done
}

# Clean up S3 backups using GFS retention
cleanup_old_s3_backups() {
    if [[ -z "$S3_BUCKET" ]]; then
        return 0
    fi

    log_info "Applying GFS retention to S3 (daily=${KEEP_DAILY}, weekly=${KEEP_WEEKLY}, monthly=${KEEP_MONTHLY})..."

    # Collect all S3 backup files with their dates
    local -A file_by_date=()   # date -> space-separated filenames
    local all_dates=()

    while IFS= read -r line; do
        local filename
        filename=$(echo "$line" | awk '{print $4}')
        [[ -z "$filename" ]] && continue

        local file_date
        file_date=$(echo "$filename" | grep -oP '\d{8}(?=_\d{6})' | head -1)
        [[ -z "$file_date" ]] && continue

        if [[ -z "${file_by_date[$file_date]:-}" ]]; then
            file_by_date["$file_date"]="$filename"
            all_dates+=("$file_date")
        else
            file_by_date["$file_date"]+=" $filename"
        fi
    done < <(aws s3 ls "${S3_BUCKET}/" 2>/dev/null)

    if [[ ${#all_dates[@]} -eq 0 ]]; then
        log_info "No S3 backups found"
        return 0
    fi

    # Sort dates newest first
    local sorted_dates
    sorted_dates=$(printf '%s\n' "${all_dates[@]}" | sort -rn)

    # Compute which dates to keep
    local -A keep_dates=()
    while IFS= read -r d; do
        keep_dates["$d"]=1
    done < <(echo "$sorted_dates" | compute_keep_set)

    # Delete files whose dates are not in the keep set
    local deleted_count=0
    for d in "${all_dates[@]}"; do
        if [[ -z "${keep_dates[$d]:-}" ]]; then
            for filename in ${file_by_date[$d]}; do
                log_info "Deleting S3 backup: ${filename} (date: ${d})"
                if aws s3 rm "${S3_BUCKET}/${filename}" --quiet; then
                    deleted_count=$((deleted_count + 1))
                fi
            done
        fi
    done

    if [[ $deleted_count -gt 0 ]]; then
        log_success "Deleted ${deleted_count} old S3 backup(s)"
    else
        log_info "No old S3 backups to delete"
    fi

    # Show what's kept
    local kept_count=0
    for _ in "${!keep_dates[@]}"; do kept_count=$((kept_count + 1)); done
    log_info "Keeping ${kept_count} S3 backup date(s)"
}

# Clean up local backups using GFS retention
cleanup_old_backups() {
    local parent_backup_dir
    parent_backup_dir=$(dirname "${BACKUP_DIR}")

    if [[ ! -d "$parent_backup_dir" ]]; then
        log_info "No old backups to clean up"
        return 0
    fi

    log_info "Applying GFS retention to local backups (daily=${KEEP_DAILY}, weekly=${KEEP_WEEKLY}, monthly=${KEEP_MONTHLY})..."

    # Collect all backup directories with their dates
    local -A dir_by_date=()
    local all_dates=()

    while IFS= read -r -d '' dir; do
        local dirname
        dirname=$(basename "$dir")
        # Extract date from directory name: backup_YYYYMMDD_HHMMSS
        local dir_date
        dir_date=$(echo "$dirname" | grep -oP '\d{8}(?=_\d{6})' | head -1)
        [[ -z "$dir_date" ]] && continue

        if [[ -z "${dir_by_date[$dir_date]:-}" ]]; then
            dir_by_date["$dir_date"]="$dir"
            all_dates+=("$dir_date")
        else
            dir_by_date["$dir_date"]+=" $dir"
        fi
    done < <(find "$parent_backup_dir" -maxdepth 1 -type d -name "backup_*" -print0)

    if [[ ${#all_dates[@]} -eq 0 ]]; then
        log_info "No local backups found"
        return 0
    fi

    # Sort dates newest first
    local sorted_dates
    sorted_dates=$(printf '%s\n' "${all_dates[@]}" | sort -rn)

    # Compute which dates to keep
    local -A keep_dates=()
    while IFS= read -r d; do
        keep_dates["$d"]=1
    done < <(echo "$sorted_dates" | compute_keep_set)

    # Delete directories whose dates are not in the keep set
    local deleted_count=0
    for d in "${all_dates[@]}"; do
        if [[ -z "${keep_dates[$d]:-}" ]]; then
            for dir in ${dir_by_date[$d]}; do
                log_info "Deleting local backup: ${dir} (date: ${d})"
                rm -rf "$dir"
                deleted_count=$((deleted_count + 1))
            done
        fi
    done

    if [[ $deleted_count -gt 0 ]]; then
        log_success "Deleted ${deleted_count} old local backup(s)"
    else
        log_info "No old local backups to delete"
    fi
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

        find "${BACKUP_DIR}" -type f ! -name "backup_summary.txt" -exec ls -lh {} \; | \
            awk '{print "  " $9 " (" $5 ")"}'

        echo ""
        echo "--- Total Size ---"
        du -sh "${BACKUP_DIR}" | awk '{print $1}'

    } > "$summary_file"

    log_success "Backup summary created: ${summary_file}"

    # Display summary
    cat "$summary_file"
}

# Main execution
main() {
    # Change to project root
    cd "$PROJECT_ROOT"

    # Parse arguments (before banner so --help exits cleanly)
    parse_args "$@"

    log_info "==================================="
    log_info "hybridInference Database Backup"
    log_info "==================================="
    echo ""

    # Load environment variables
    load_env || true

    # Setup backup directory
    setup_backup_dir

    # Auto-enable compression when uploading to S3 (saves bandwidth)
    if [[ -n "$S3_BUCKET" ]] && [[ "$COMPRESS" == false ]]; then
        log_info "Auto-enabling compression for S3 upload"
        COMPRESS=true
    fi

    # Backup PostgreSQL (primary database)
    if ! backup_postgres; then
        log_error "==================================="
        log_error "Backup failed!"
        log_error "==================================="
        exit 1
    fi

    # Upload to S3 if configured
    if [[ -n "$S3_BUCKET" ]]; then
        echo ""
        if ! upload_to_s3; then
            log_error "S3 upload failed!"
            exit 1
        fi
    fi

    # Create summary (skip if local files were removed)
    if [[ -d "${BACKUP_DIR}" ]]; then
        echo ""
        create_summary
    fi

    # Cleanup old backups
    echo ""
    cleanup_old_backups
    cleanup_old_s3_backups

    # Final status
    echo ""
    log_success "==================================="
    log_success "Backup completed successfully!"
    log_success "==================================="
    exit 0
}

# Run main function
main "$@"
