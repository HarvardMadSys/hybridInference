#!/bin/bash
# Database Backup Script for hybridInference
#
# This script backs up the PostgreSQL database (primary database for API logs,
# metrics, and user management).
#
# Usage:
#   ./scripts/db/backup.sh [OPTIONS]
#
# Options:
#   --retention-days N    Keep backups for N days (default: 30)
#   --backup-dir PATH     Custom backup directory (default: ./backups)
#   --compress            Compress backups with gzip
#   --help                Show this help message
#
# Environment Variables (from .env):
#   DB_NAME, DB_USER, DB_PASSWORD - PostgreSQL credentials
#
# Examples:
#   ./scripts/db/backup.sh
#   ./scripts/db/backup.sh --retention-days 7 --compress
#   ./scripts/db/backup.sh --backup-dir /mnt/backups

set -euo pipefail

# Color codes for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m' # No Color

# Default configuration
RETENTION_DAYS=30
BACKUP_DIR="./backups"
COMPRESS=false
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
            --retention-days)
                RETENTION_DAYS="$2"
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
    
    if docker exec -e PGPASSWORD="${DB_PASSWORD}" "${POSTGRES_CONTAINER}" pg_dump \
        -h localhost \
        -U "${DB_USER}" \
        -d "${DB_NAME}" \
        --clean \
        --if-exists \
        --create \
        --verbose \
        > "${backup_file}"; then
        
        local size
        size=$(du -h "${backup_file}" | cut -f1)
        log_success "PostgreSQL backup completed: ${backup_file} (${size})"
        
        # Compress if requested
        if [[ "$COMPRESS" == true ]]; then
            log_info "Compressing PostgreSQL backup..."
            gzip "${backup_file}"
            backup_file="${backup_file}.gz"
            size=$(du -h "${backup_file}" | cut -f1)
            log_success "Compressed to: ${backup_file} (${size})"
        fi
        
        return 0
    else
        log_error "PostgreSQL backup failed"
        return 1
    fi
}

# Clean up old backups
cleanup_old_backups() {
    log_info "Cleaning up backups older than ${RETENTION_DAYS} days..."
    
    local parent_backup_dir
    parent_backup_dir=$(dirname "${BACKUP_DIR}")
    
    if [[ ! -d "$parent_backup_dir" ]]; then
        log_info "No old backups to clean up"
        return 0
    fi
    
    local deleted_count=0
    
    # Find and delete old backup directories
    while IFS= read -r -d '' old_backup; do
        log_info "Deleting old backup: ${old_backup}"
        rm -rf "$old_backup"
        ((deleted_count++))
    done < <(find "$parent_backup_dir" -maxdepth 1 -type d -name "backup_*" -mtime "+${RETENTION_DAYS}" -print0)
    
    if [[ $deleted_count -gt 0 ]]; then
        log_success "Deleted ${deleted_count} old backup(s)"
    else
        log_info "No old backups to delete"
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
        echo "Retention Policy: ${RETENTION_DAYS} days"
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
    log_info "==================================="
    log_info "hybridInference Database Backup"
    log_info "==================================="
    echo ""
    
    # Change to project root
    cd "$PROJECT_ROOT"
    
    # Parse arguments
    parse_args "$@"
    
    # Load environment variables
    load_env || true
    
    # Setup backup directory
    setup_backup_dir
    
    # Backup PostgreSQL (primary database)
    if ! backup_postgres; then
        log_error "==================================="
        log_error "Backup failed!"
        log_error "==================================="
        exit 1
    fi
    
    # Create summary
    echo ""
    create_summary
    
    # Cleanup old backups
    echo ""
    cleanup_old_backups
    
    # Final status
    echo ""
    log_success "==================================="
    log_success "Backup completed successfully!"
    log_success "==================================="
    exit 0
}

# Run main function
main "$@"
