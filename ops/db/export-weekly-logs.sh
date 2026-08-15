#!/bin/bash
# Weekly api_logs JSONL export + gated prune for hybridInference
#
# Exports the previous complete UTC ISO week (Monday 00:00 through the next
# Monday 00:00, exclusive) with ops/db/export_logs.py, copies the archive to a
# remote host, and only then — if a fresh S3 database backup is also in place —
# deletes api_logs rows older than a retention window via archive-old-logs.sh.
#
# The copy is streamed over ssh into a .partial file and promoted only after a
# remote zstd integrity check. A week of api_logs is large enough that writing
# it locally on the Postgres volume and then scp'ing it can fill the disk and
# take the database down; the destination path is the one scp would have used.
#
# Safety model (deletion never runs unless all of these hold):
#   1. The remote archive is written and passes a zstd integrity check, or an
#      already-promoted file for this week is present and still verifies.
#   2. check-backup-health.sh reports a fresh S3 backup (disk check disabled
#      so a low-space condition cannot block the prune that frees space).
#   3. archive-old-logs.sh then archives the old rows to S3, verifies the
#      upload, and only then DELETEs. See that script for its own guards.
#
# NOTE: api_logs contains user prompts/responses (PII). The remote dest and
# the --s3-archive target must be locations you are authorized to store that
# data in.
#
# Usage:
#   ./ops/db/export-weekly-logs.sh [OPTIONS]
#
# Options:
#   --since DATE          Inclusive start day (YYYY-MM-DD). Default: Monday of
#                         the previous complete UTC ISO week
#   --until DATE          Exclusive end day (YYYY-MM-DD). Default: this week's
#                         Monday (so Sunday is the last included day)
#   --remote USER@HOST    ssh/scp target (required unless --dry-run)
#   --port N              ssh/scp port (default: 22)
#   --dest-dir PATH       Remote directory for the archive
#   --retention-days N    Prune rows older than N days (default: 30)
#   --s3-archive URI      S3 prefix passed to archive-old-logs.sh. Required
#                         to prune; omit to export+copy only
#   --s3-backup URI       Backup bucket that must look fresh before prune
#                         (default: s3://harvardsys-backup/freeinference)
#   --max-backup-age-hours N
#                         Backup older than this blocks prune (default: 26)
#   --env-file PATH       .env for export_logs.py (default: auto-detect)
#   --force               Re-export even if this week's remote file exists
#   --skip-prune          Copy only; do not delete old rows
#   --dry-run             Print the week, dest, and planned steps; write nothing
#   --help                Show this help message
#
# Environment variables:
#   EXPORT_PYTHON         Python interpreter (default: $PROJECT_ROOT/.venv/bin/python)
#   EXPORT_SSH            ssh binary (overridable in tests)
#   EXPORT_LOGS_PY        Path to export_logs.py
#   EXPORT_ARCHIVE_SCRIPT Path to archive-old-logs.sh
#   EXPORT_BACKUP_HEALTH_SCRIPT
#                         Path to check-backup-health.sh
#   EXPORT_PROJECT_ROOT   Override the detected project root
#
# Examples:
#   ./ops/db/export-weekly-logs.sh --dry-run --remote user@host --dest-dir /data/api-log-exports
#   ./ops/db/export-weekly-logs.sh --remote user@host --port 10021 \
#       --dest-dir /data/api-log-exports --s3-archive s3://bucket/archive/api_logs

set -euo pipefail

readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m'

SINCE=""
UNTIL=""
REMOTE=""
SSH_PORT=22
DEST_DIR=""
RETENTION_DAYS=30
S3_ARCHIVE=""
S3_BACKUP="s3://harvardsys-backup/freeinference"
MAX_BACKUP_AGE_HOURS=26
ENV_FILE=""
FORCE=false
SKIP_PRUNE=false
DRY_RUN=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${EXPORT_PROJECT_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
EXPORT_LOGS_PY="${EXPORT_LOGS_PY:-${SCRIPT_DIR}/export_logs.py}"
ARCHIVE_SCRIPT="${EXPORT_ARCHIVE_SCRIPT:-${SCRIPT_DIR}/archive-old-logs.sh}"
BACKUP_HEALTH_SCRIPT="${EXPORT_BACKUP_HEALTH_SCRIPT:-${SCRIPT_DIR}/check-backup-health.sh}"
SSH_BIN="${EXPORT_SSH:-ssh}"

START_DAY=""
END_DAY=""
UNTIL_EXCL=""
REMOTE_FINAL=""
REMOTE_PARTIAL=""
COPY_OK=false

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

die() {
    log_error "$*"
    exit 1
}

show_help() {
    sed -n '2,/^$/{ s/^# \?//; p }' "$0"
    exit 0
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --since)
                SINCE="$2"
                shift 2
                ;;
            --until)
                UNTIL="$2"
                shift 2
                ;;
            --remote)
                REMOTE="$2"
                shift 2
                ;;
            --port)
                SSH_PORT="$2"
                shift 2
                ;;
            --dest-dir)
                DEST_DIR="$2"
                shift 2
                ;;
            --retention-days)
                RETENTION_DAYS="$2"
                shift 2
                ;;
            --s3-archive)
                S3_ARCHIVE="$2"
                shift 2
                ;;
            --s3-backup)
                S3_BACKUP="$2"
                shift 2
                ;;
            --max-backup-age-hours)
                MAX_BACKUP_AGE_HOURS="$2"
                shift 2
                ;;
            --env-file)
                ENV_FILE="$2"
                shift 2
                ;;
            --force)
                FORCE=true
                shift
                ;;
            --skip-prune)
                SKIP_PRUNE=true
                shift
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --help | -h)
                show_help
                ;;
            *)
                die "Unknown option: $1 (use --help)"
                ;;
        esac
    done
}

is_ymd() {
    [[ "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]
}

# Previous complete UTC ISO week → START_DAY, END_DAY (inclusive), UNTIL_EXCL.
compute_default_week() {
    local today dow this_monday
    today=$(date -u +%Y-%m-%d)
    dow=$(date -u +%u)
    this_monday=$(date -u -d "${today} -$((dow - 1)) days" +%Y-%m-%d)
    START_DAY=$(date -u -d "${this_monday} - 7 days" +%Y-%m-%d)
    END_DAY=$(date -u -d "${this_monday} - 1 day" +%Y-%m-%d)
    UNTIL_EXCL="$this_monday"
}

resolve_window() {
    if [[ -n "$SINCE" || -n "$UNTIL" ]]; then
        [[ -n "$SINCE" && -n "$UNTIL" ]] || die "--since and --until must be passed together"
        is_ymd "$SINCE" || die "--since must be YYYY-MM-DD (got: ${SINCE})"
        is_ymd "$UNTIL" || die "--until must be YYYY-MM-DD (got: ${UNTIL})"
        [[ "$UNTIL" > "$SINCE" ]] || die "--until must be after --since"
        START_DAY="$SINCE"
        UNTIL_EXCL="$UNTIL"
        END_DAY=$(date -u -d "${UNTIL_EXCL} - 1 day" +%Y-%m-%d)
    else
        compute_default_week
    fi

    [[ -n "$DEST_DIR" ]] || die "--dest-dir is required"
    if ! [[ "$SSH_PORT" =~ ^[0-9]+$ ]] || [[ "$SSH_PORT" -eq 0 ]]; then
        die "--port must be a positive integer (got: ${SSH_PORT})"
    fi
    if ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || [[ "$RETENTION_DAYS" -eq 0 ]]; then
        die "--retention-days must be a positive integer (got: ${RETENTION_DAYS})"
    fi
    if ! [[ "$MAX_BACKUP_AGE_HOURS" =~ ^[0-9]+$ ]] || [[ "$MAX_BACKUP_AGE_HOURS" -eq 0 ]]; then
        die "--max-backup-age-hours must be a positive integer (got: ${MAX_BACKUP_AGE_HOURS})"
    fi
    if [[ "$DRY_RUN" == false && -z "$REMOTE" ]]; then
        die "--remote user@host is required (or pass --dry-run)"
    fi

    local base
    base="api_logs_${START_DAY}_${END_DAY}.jsonl.zst"
    REMOTE_FINAL="${DEST_DIR%/}/${base}"
    REMOTE_PARTIAL="${REMOTE_FINAL}.partial"
}

resolve_python() {
    if [[ -n "${EXPORT_PYTHON:-}" ]]; then
        printf '%s' "$EXPORT_PYTHON"
        return
    fi
    if [[ -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
        printf '%s' "${PROJECT_ROOT}/.venv/bin/python"
        return
    fi
    if command -v python3 &> /dev/null; then
        command -v python3
        return
    fi
    die "No Python interpreter found (set EXPORT_PYTHON or create ${PROJECT_ROOT}/.venv)"
}

ssh_remote() {
    "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=20 \
        -o StrictHostKeyChecking=accept-new \
        -p "$SSH_PORT" "$REMOTE" "$1"
}

remote_mkdir() {
    ssh_remote "mkdir -p '${DEST_DIR}'"
}

remote_final_ok() {
    ssh_remote "test -f '${REMOTE_FINAL}' && zstd -q -t '${REMOTE_FINAL}'"
}

stream_export() {
    local python_bin
    python_bin=$(resolve_python)
    [[ -f "$EXPORT_LOGS_PY" ]] || die "export_logs.py not found at ${EXPORT_LOGS_PY}"
    command -v "$SSH_BIN" &> /dev/null || die "ssh not found (needed to copy to ${REMOTE})"
    command -v zstd &> /dev/null || log_warning "local zstd not found; remote zstd is still required"

    log_info "Ensuring remote directory ${DEST_DIR} on ${REMOTE}:${SSH_PORT} ..."
    remote_mkdir

    if [[ "$FORCE" == false ]] && remote_final_ok; then
        log_success "Remote archive already present and verifies: ${REMOTE_FINAL}"
        COPY_OK=true
        return 0
    fi

    ssh_remote "rm -f '${REMOTE_PARTIAL}'" || true

    log_info "Exporting ${START_DAY} .. ${END_DAY} and streaming to ${REMOTE}:${REMOTE_PARTIAL} ..."
    local env_args=()
    if [[ -n "$ENV_FILE" ]]; then
        env_args+=(--env-file "$ENV_FILE")
    fi

    set +e
    "$python_bin" "$EXPORT_LOGS_PY" \
        --since "$START_DAY" \
        --until "$UNTIL_EXCL" \
        "${env_args[@]}" \
        -o - \
        | ssh_remote "cat > '${REMOTE_PARTIAL}'"
    local -a pipe_rc=("${PIPESTATUS[@]}")
    set -e

    if [[ "${pipe_rc[0]}" -ne 0 || "${pipe_rc[1]}" -ne 0 ]]; then
        ssh_remote "rm -f '${REMOTE_PARTIAL}'" || true
        die "export/copy failed (export_logs.py exit ${pipe_rc[0]}, ssh exit ${pipe_rc[1]})"
    fi

    if ! ssh_remote "zstd -q -t '${REMOTE_PARTIAL}' && mv -f '${REMOTE_PARTIAL}' '${REMOTE_FINAL}'"; then
        ssh_remote "rm -f '${REMOTE_PARTIAL}'" || true
        die "remote archive failed zstd check; left nothing at ${REMOTE_FINAL}"
    fi

    local remote_bytes
    remote_bytes=$(ssh_remote "stat -c %s '${REMOTE_FINAL}'")
    [[ "$remote_bytes" =~ ^[0-9]+$ ]] || die "could not stat remote archive"
    if [[ "$remote_bytes" -eq 0 ]]; then
        ssh_remote "rm -f '${REMOTE_FINAL}'" || true
        die "remote archive is 0 bytes; refusing to treat the copy as success"
    fi

    log_success "Copied and verified: ${REMOTE}:${REMOTE_FINAL} (${remote_bytes} bytes)"
    COPY_OK=true
}

backup_is_fresh() {
    [[ -x "$BACKUP_HEALTH_SCRIPT" || -f "$BACKUP_HEALTH_SCRIPT" ]] \
        || die "check-backup-health.sh not found at ${BACKUP_HEALTH_SCRIPT}"
    # --min-free-gib 0: a low-disk condition must not block the prune that
    # is supposed to reclaim table space. --dry-run: do not Slack from here.
    "$BACKUP_HEALTH_SCRIPT" \
        --s3-bucket "$S3_BACKUP" \
        --max-age-hours "$MAX_BACKUP_AGE_HOURS" \
        --min-free-gib 0 \
        --dry-run
}

prune_old_rows() {
    [[ -f "$ARCHIVE_SCRIPT" ]] || die "archive-old-logs.sh not found at ${ARCHIVE_SCRIPT}"
    [[ -n "$S3_ARCHIVE" ]] || die "--s3-archive is required to prune (or pass --skip-prune)"
    log_info "Pruning api_logs rows older than ${RETENTION_DAYS} days ..."
    "$ARCHIVE_SCRIPT" \
        --retention-days "$RETENTION_DAYS" \
        --s3-archive "$S3_ARCHIVE"
}

main() {
    cd "$PROJECT_ROOT"
    parse_args "$@"
    resolve_window

    log_info "==================================="
    log_info "hybridInference weekly api_logs export"
    log_info "==================================="
    log_info "Window:     ${START_DAY} .. ${END_DAY} (until ${UNTIL_EXCL} exclusive)"
    log_info "Remote:     ${REMOTE:-<none>}:${SSH_PORT}"
    log_info "Dest:       ${REMOTE_FINAL}"
    log_info "Retention:  ${RETENTION_DAYS} days (prune after copy + fresh backup)"
    echo ""

    if [[ "$DRY_RUN" == true ]]; then
        log_warning "DRY RUN — would export ${START_DAY}..${END_DAY} to ${REMOTE:-local}:${REMOTE_FINAL}"
        if [[ "$SKIP_PRUNE" == true ]]; then
            log_warning "DRY RUN — prune skipped (--skip-prune)"
        else
            log_warning "DRY RUN — would prune rows older than ${RETENTION_DAYS} days if the copy and backup both succeed"
        fi
        exit 0
    fi

    stream_export

    if [[ "$COPY_OK" != true ]]; then
        die "refusing to prune: the weekly copy did not succeed"
    fi

    if [[ "$SKIP_PRUNE" == true ]]; then
        log_info "Skipping prune (--skip-prune)"
        log_success "Weekly export complete (prune skipped)"
        exit 0
    fi

    if [[ -z "$S3_ARCHIVE" ]]; then
        log_warning "No --s3-archive set; exported but did not prune"
        exit 0
    fi

    log_info "Checking that a fresh database backup exists before prune ..."
    if ! backup_is_fresh; then
        die "weekly copy succeeded, but the database backup is not fresh — leaving rows in place"
    fi
    log_success "Backup looks fresh; proceeding to prune"

    prune_old_rows

    echo ""
    log_success "==================================="
    log_success "Exported ${START_DAY}..${END_DAY} and pruned rows older than ${RETENTION_DAYS} days"
    log_success "==================================="
}

main "$@"
