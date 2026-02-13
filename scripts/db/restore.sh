#!/bin/bash
# Database Restore Script for hybridInference
#
# This script restores PostgreSQL and SQLite databases from backups.
#
# Usage:
#   ./scripts/db/restore.sh [OPTIONS]
#
# Options:
#   --backup-dir PATH     Backup directory to restore from (required)
#   --postgres-only       Only restore PostgreSQL
#   --sqlite-only         Only restore SQLite databases
#   --force               Skip confirmation prompts (dangerous!)
#   --help                Show this help message
#
# Environment Variables (from .env):
#   DB_NAME, DB_USER, DB_PASSWORD - PostgreSQL credentials
#
# Examples:
#   ./scripts/db/restore.sh --backup-dir ./backups/backup_20241114_120000
#   ./scripts/db/restore.sh --postgres-only --backup-dir ./backups/backup_20241114_120000
#
# WARNING: This will OVERWRITE existing databases!

set -euo pipefail

# Color codes for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m' # No Color

# Default configuration
BACKUP_DIR=""
POSTGRES_RESTORE=true
SQLITE_RESTORE=false
FORCE=false
DOCKER_COMPOSE_FILE="infrastructure/docker/docker-compose.yml"
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

# Show help message
show_help() {
    grep '^#' "$0" | grep -v '#!/bin/bash' | sed 's/^# //' | sed 's/^#//'
    exit 0
}

# Parse command line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --backup-dir)
                BACKUP_DIR="$2"
                shift 2
                ;;
            --postgres-only)
                POSTGRES_RESTORE=true
                SQLITE_RESTORE=false
                shift
                ;;
            --sqlite-only)
                POSTGRES_RESTORE=false
                SQLITE_RESTORE=true
                shift
                ;;
            --force)
                FORCE=true
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

    # Validate required arguments
    if [[ -z "$BACKUP_DIR" ]]; then
        log_error "--backup-dir is required"
        echo "Use --help for usage information"
        exit 1
    fi

    if [[ ! -d "$BACKUP_DIR" ]]; then
        log_error "Backup directory does not exist: ${BACKUP_DIR}"
        exit 1
    fi
}

# Load environment variables from .env file
load_env() {
    local env_file="${PROJECT_ROOT}/.env"

    if [[ ! -f "$env_file" ]]; then
        log_warning ".env file not found at ${env_file}"
        log_warning "PostgreSQL restore may fail without credentials"
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

# Confirm restore operation
confirm_restore() {
    if [[ "$FORCE" == true ]]; then
        return 0
    fi

    echo ""
    log_warning "==================================="
    log_warning "WARNING: DATABASE RESTORE"
    log_warning "==================================="
    log_warning "This will OVERWRITE existing databases!"
    log_warning "Backup directory: ${BACKUP_DIR}"
    echo ""

    read -p "Are you sure you want to continue? (type 'yes' to confirm): " -r
    echo ""

    if [[ ! $REPLY =~ ^yes$ ]]; then
        log_info "Restore cancelled by user"
        exit 0
    fi
}

# Restore PostgreSQL database
restore_postgres() {
    log_info "Starting PostgreSQL restore..."

    if ! check_postgres_container; then
        return 1
    fi

    # Validate required environment variables
    if [[ -z "${DB_NAME:-}" ]] || [[ -z "${DB_USER:-}" ]] || [[ -z "${DB_PASSWORD:-}" ]]; then
        log_error "DB_NAME, DB_USER, and DB_PASSWORD must be set in .env file"
        return 1
    fi

    # Find the most recent PostgreSQL backup
    # First try root directory, then fallback to postgres subdirectory
    local backup_file
    backup_file=$(find "${BACKUP_DIR}" -maxdepth 1 -type f \( -name "*.sql" -o -name "*.sql.gz" \) | sort -r | head -n 1)

    if [[ -z "$backup_file" && -d "${BACKUP_DIR}/postgres" ]]; then
        backup_file=$(find "${BACKUP_DIR}/postgres" -type f \( -name "*.sql" -o -name "*.sql.gz" \) | sort -r | head -n 1)
    fi

    if [[ -z "$backup_file" ]]; then
        log_error "No PostgreSQL backup found in ${BACKUP_DIR}"
        return 1
    fi

    log_info "Found backup: ${backup_file}"

    # Decompress if needed
    local temp_file=""
    if [[ "$backup_file" == *.gz ]]; then
        log_info "Decompressing backup..."
        temp_file="${backup_file%.gz}"
        gunzip -c "$backup_file" > "$temp_file"
        backup_file="$temp_file"
    fi

    # Restore database with password and host
    log_info "Restoring database '${DB_NAME}'..."

    if docker exec -e PGPASSWORD="${DB_PASSWORD}" -i "${POSTGRES_CONTAINER}" psql \
        -h localhost \
        -U "${DB_USER}" \
        -d postgres \
        < "$backup_file"; then

        log_success "PostgreSQL restore completed successfully"

        # Clean up temp file
        if [[ -n "$temp_file" ]]; then
            rm -f "$temp_file"
        fi

        return 0
    else
        log_error "PostgreSQL restore failed"

        # Clean up temp file
        if [[ -n "$temp_file" ]]; then
            rm -f "$temp_file"
        fi

        return 1
    fi
}

# Restore SQLite databases
restore_sqlite() {
    log_info "Starting SQLite restore..."

    local sqlite_backup_dir="${BACKUP_DIR}/sqlite"

    if [[ ! -d "$sqlite_backup_dir" ]]; then
        log_error "SQLite backup directory not found: ${sqlite_backup_dir}"
        return 1
    fi

    local restore_count=0

    # Find all .db files (and .db.gz files)
    while IFS= read -r -d '' backup_file; do
        local db_name
        db_name=$(basename "$backup_file")

        # Remove timestamp from filename to get original name
        # Format: openrouter_logs.db_20241114_120000.db -> openrouter_logs.db
        local original_name
        original_name=$(echo "$db_name" | sed -E 's/(.*)_[0-9]{8}_[0-9]{6}\.db(\.gz)?$/\1/')

        # Determine target directory based on original location
        local target_dir
        if [[ -d "${PROJECT_ROOT}/data/db" ]]; then
            target_dir="${PROJECT_ROOT}/data/db"
        else
            target_dir="${PROJECT_ROOT}/var/db"
        fi

        local target_file="${target_dir}/${original_name}"

        log_info "Restoring: ${original_name}"

        # Decompress if needed
        local temp_file=""
        if [[ "$backup_file" == *.gz ]]; then
            log_info "Decompressing backup..."
            temp_file="${backup_file%.gz}"
            gunzip -c "$backup_file" > "$temp_file"
            backup_file="$temp_file"
        fi

        # Create target directory if it doesn't exist
        mkdir -p "$target_dir"

        # Copy backup to target location
        if cp "$backup_file" "$target_file"; then
            log_success "SQLite restore completed: ${target_file}"
            ((restore_count++))
        else
            log_error "Failed to restore: ${original_name}"
        fi

        # Clean up temp file
        if [[ -n "$temp_file" ]]; then
            rm -f "$temp_file"
        fi

    done < <(find "$sqlite_backup_dir" -type f \( -name "*.db" -o -name "*.db.gz" \) -print0)

    if [[ $restore_count -eq 0 ]]; then
        log_warning "No SQLite databases found to restore"
        return 1
    fi

    log_success "Restored ${restore_count} SQLite database(s)"
    return 0
}

# Main execution
main() {
    log_info "==================================="
    log_info "hybridInference Database Restore"
    log_info "==================================="
    echo ""

    # Change to project root
    cd "$PROJECT_ROOT"

    # Parse arguments
    parse_args "$@"

    # Load environment variables
    load_env || true

    # Confirm restore
    confirm_restore

    local restore_success=true

    # Restore PostgreSQL
    if [[ "$POSTGRES_RESTORE" == true ]]; then
        if ! restore_postgres; then
            restore_success=false
        fi
    fi

    # Restore SQLite
    if [[ "$SQLITE_RESTORE" == true ]]; then
        if ! restore_sqlite; then
            restore_success=false
        fi
    fi

    # Final status
    echo ""
    if [[ "$restore_success" == true ]]; then
        log_success "==================================="
        log_success "Restore completed successfully!"
        log_success "==================================="
        exit 0
    else
        log_warning "==================================="
        log_warning "Restore completed with warnings"
        log_warning "==================================="
        exit 1
    fi
}

# Run main function
main "$@"
