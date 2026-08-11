#!/bin/bash
# Backup and free-space monitor for hybridInference
#
# Answers two questions the nightly backup cannot answer about itself, and
# posts to chat when either goes wrong:
#
#   1. Is there a recent, plausibly-sized backup in S3?
#   2. Is there still enough free space on the volume Postgres lives on?
#
# Usage:
#   ./ops/db/check-backup-health.sh [OPTIONS]
#
# Options:
#   --s3-bucket URI       Backup location to inspect
#                         (default: s3://harvardsys-backup/freeinference)
#   --max-age-hours N     Alert if the newest backup is older than this
#                         (default: 26 — the 04:00 job plus a 2h grace)
#   --min-free-gib N      Alert if free space drops below this (default: 80)
#   --min-size-pct N      Alert if the newest backup is smaller than this
#                         percent of the one before it (default: 50)
#   --mount PATH          Filesystem to check for free space (default: /)
#   --cooldown-sec N      Minimum gap between repeat alerts for the same
#                         condition (default: 21600 — 6h)
#   --dry-run             Evaluate and report, but post nothing
#   --test                Post a test alert and exit, to prove the webhook works
#   --help                Show this help message
#
# Environment variables:
#   MONITOR_ALERT_WEBHOOK_URL   Where to post. Falls back to
#                               SLACK_ALERTS_WEBHOOK_URL then SLACK_WEBHOOK_URL
#                               read from MONITOR_ENV_FILE.
#   MONITOR_ENV_FILE            .env to read the webhook from
#                               (default: /srv/hybridInference/.env)
#   MONITOR_STATE_DIR           Where repeat-alert cooldowns are tracked
#                               (default: ~/.cache/hybridinference-monitor)
#
# Why this exists separately from backup.sh:
#   backup.sh's notify_failure() reports a run that started and then broke. It
#   cannot report the two failures that actually went unnoticed for days: a run
#   that never fired at all, and a run that "succeeded" with a fraction of the
#   data. Anything that watches a job has to live outside that job.
#
# Why S3 is the source of truth:
#   Not ~freeinference/backup.log — that rotates, and a run that never fired
#   writes nothing to it, which is indistinguishable from a quiet success. The
#   uploaded object is the only artifact that matters for a restore, so its
#   existence, age, and size are what get checked.
#
# Why the size baseline is the previous object, not the largest:
#   It was the largest, on the theory that this database only grows and that
#   comparing against the immediately-previous backup lets two consecutive
#   truncated dumps hide each other. The premise is false. Archiving api_logs
#   rows to S3 and deleting them (ops/db/archive-old-logs.sh) is a normal
#   maintenance operation, and it took the dump from 129 GiB to 4 GiB on
#   2026-08-08. Because GFS retention keeps the pre-archival objects for weeks
#   as the weekly and monthly copies, a largest-object baseline then reports
#   every healthy backup as a critical failure until they age out — which is
#   how a monitor teaches its readers to ignore it.
#
#   The blind spot that motivated it is covered where it belongs: backup.sh
#   asserts pg_dump's own end-of-dump sentinel and a minimum object size before
#   promoting the .partial key, so a truncated dump is rejected at write time
#   and never becomes an object to compare against. What is left for this check
#   is a step change worth a human look, which is a warning, not a page — and
#   one that goes quiet on its own once the new size is the norm.
#
# Examples:
#   ./ops/db/check-backup-health.sh
#   ./ops/db/check-backup-health.sh --dry-run
#   ./ops/db/check-backup-health.sh --test
#   ./ops/db/check-backup-health.sh --min-free-gib 120 --max-age-hours 30

set -Eeuo pipefail

# Color codes for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m' # No Color

# Default configuration
S3_URI="s3://harvardsys-backup/freeinference"
MAX_AGE_HOURS=26
MIN_FREE_GIB=80
MIN_SIZE_PCT=50
MOUNT="/"
COOLDOWN_SEC=21600
DRY_RUN=false
TEST_ONLY=false

ENV_FILE="${MONITOR_ENV_FILE:-/srv/hybridInference/.env}"
STATE_DIR="${MONITOR_STATE_DIR:-${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}/hybridinference-monitor}"

readonly GIB=1073741824
# Objects the backup is expected to leave behind. Excludes the .partial keys a
# failed upload discards, which are not restorable backups.
readonly OBJECT_RE='freeinference_db_[0-9]{8}_[0-9]{6}\.sql\.zst$'

# Set when any check fails, so the exit status reflects the outcome even when a
# cooldown suppressed the message.
PROBLEMS=0

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

json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/ }"
    s="${s//$'\r'/ }"
    s="${s//$'\t'/ }"
    printf '%s' "$s"
}

# ============================================================================
# Alert delivery
# ============================================================================

# Reads the webhook without ever echoing it. Accepts optional surrounding
# quotes, which .env files carry and curl would otherwise send as part of the
# URL.
resolve_webhook() {
    local url="${MONITOR_ALERT_WEBHOOK_URL:-}"
    if [[ -n "$url" ]]; then
        printf '%s' "$url"
        return 0
    fi
    if [[ ! -r "$ENV_FILE" ]]; then
        return 0
    fi
    local key
    for key in SLACK_ALERTS_WEBHOOK_URL SLACK_WEBHOOK_URL; do
        url=$(grep -E "^${key}=" "$ENV_FILE" 2> /dev/null | tail -n 1 | cut -d= -f2-) || true
        url="${url%\"}"
        url="${url#\"}"
        url="${url%\'}"
        url="${url#\'}"
        if [[ -n "$url" ]]; then
            printf '%s' "$url"
            return 0
        fi
    done
}

# True when this condition alerted recently. Keeps an hourly timer from turning
# a days-long disk problem into 24 identical messages a day, without silencing
# the condition entirely.
in_cooldown() {
    local key="$1" state_file last now
    state_file="${STATE_DIR}/${key}.last"
    [[ -f "$state_file" ]] || return 1
    last=$(cat "$state_file" 2> /dev/null) || return 1
    [[ "$last" =~ ^[0-9]+$ ]] || return 1
    now=$(date -u +%s)
    ((now - last < COOLDOWN_SEC))
}

record_alert() {
    local key="$1"
    mkdir -p "$STATE_DIR" 2> /dev/null || true
    date -u +%s > "${STATE_DIR}/${key}.last" 2> /dev/null || true
}

# Clears a condition's cooldown once it recovers, so the next occurrence alerts
# immediately instead of being swallowed by a stale timer.
clear_alert() {
    local key="$1"
    rm -f "${STATE_DIR}/${key}.last" 2> /dev/null || true
}

send_alert() {
    local key="$1" severity="$2" text="$3"
    PROBLEMS=$((PROBLEMS + 1))
    log_error "$text"

    if [[ "$DRY_RUN" == true ]]; then
        log_info "  dry run: would post alert '${key}'"
        return 0
    fi
    local url
    url=$(resolve_webhook)
    if [[ -z "$url" ]]; then
        log_warning "  no webhook configured (MONITOR_ALERT_WEBHOOK_URL / ${ENV_FILE}); alert not sent"
        return 0
    fi
    if ! command -v curl &> /dev/null; then
        log_warning "  curl is missing; alert not sent"
        return 0
    fi
    if in_cooldown "$key"; then
        log_info "  within ${COOLDOWN_SEC}s cooldown for '${key}'; not re-sending"
        return 0
    fi

    local payload http_code icon
    icon=":rotating_light:"
    [[ "$severity" == "warning" ]] && icon=":warning:"
    payload=$(printf '{"text":"%s %s"}' "$icon" "$(json_escape "$text")")

    # -f matters: without it curl exits 0 on a 4xx/5xx, so a revoked or typo'd
    # webhook would be logged as a delivered alert. Same reasoning as
    # backup.sh's notify_failure — an alert path that lies about delivering is
    # worse than none.
    http_code=0
    if http_code=$(curl -fsS -m 15 --retry 2 --retry-delay 2 \
        -X POST -H 'Content-Type: application/json' \
        -d "$payload" "$url" -o /dev/null -w '%{http_code}'); then
        log_info "  alert delivered (HTTP ${http_code})"
        record_alert "$key"
    else
        log_error "  alert POST FAILED (HTTP ${http_code:-none}) — this problem is unreported"
    fi
}

# ============================================================================
# Checks
# ============================================================================

check_backup() {
    log_info "Checking backups in ${S3_URI} ..."

    local listing rc=0
    # A listing we cannot read is itself an alert: expired credentials or a
    # renamed bucket would otherwise look exactly like a healthy quiet run.
    listing=$(aws s3 ls "${S3_URI}/" 2>&1) || rc=$?
    if ((rc != 0)); then
        local detail
        detail=$(printf '%s' "$listing" | tail -n 1)
        # A missing prefix exits non-zero with no output at all, so the message
        # has to stand on its own rather than trailing off after "aws s3 ls:".
        [[ -n "$detail" ]] || detail="exit ${rc}, no output — prefix missing, or no permission to list it"
        send_alert "backup_unreadable" critical \
            "DB backup check FAILED: cannot list ${S3_URI} (aws s3 ls: ${detail}). Backup state is unknown."
        return 0
    fi

    local objects
    objects=$(printf '%s\n' "$listing" | grep -E "$OBJECT_RE" | sort -k1,2) || true
    if [[ -z "$objects" ]]; then
        send_alert "backup_missing" critical \
            "DB backup check FAILED: no backup objects found in ${S3_URI}. There is nothing to restore from."
        return 0
    fi

    local newest newest_date newest_time newest_size newest_key prev_size
    newest=$(printf '%s\n' "$objects" | tail -n 1)
    newest_date=$(awk '{print $1}' <<< "$newest")
    newest_time=$(awk '{print $2}' <<< "$newest")
    newest_size=$(awk '{print $3}' <<< "$newest")
    newest_key=$(awk '{print $4}' <<< "$newest")
    # Empty when this is the only backup, which the size check treats as
    # nothing to compare rather than as a shrink from zero.
    prev_size=$(printf '%s\n' "$objects" | tail -n 2 | head -n 1 | awk '{print $3}')
    [[ "$newest_size" == "$prev_size" && $(printf '%s\n' "$objects" | wc -l) -eq 1 ]] && prev_size=""

    local newest_epoch now age_hours
    if ! newest_epoch=$(date -u -d "${newest_date} ${newest_time} UTC" +%s 2> /dev/null); then
        send_alert "backup_unreadable" critical \
            "DB backup check FAILED: could not parse the timestamp of ${newest_key} ('${newest_date} ${newest_time}')."
        return 0
    fi
    now=$(date -u +%s)
    age_hours=$(((now - newest_epoch) / 3600))

    log_info "  newest: ${newest_key}"
    log_info "  age:    ${age_hours}h (alert above ${MAX_AGE_HOURS}h)"
    if [[ -n "$prev_size" ]]; then
        log_info "  size:   $((newest_size / GIB)) GiB (previous: $((prev_size / GIB)) GiB)"
    else
        log_info "  size:   $((newest_size / GIB)) GiB (no previous backup to compare)"
    fi

    # Staleness covers both a run that failed and a run that never fired, which
    # is the case cron and notify_failure are both blind to.
    if ((age_hours > MAX_AGE_HOURS)); then
        send_alert "backup_stale" critical \
            "DB backup is STALE: newest backup ${newest_key} is ${age_hours}h old (threshold ${MAX_AGE_HOURS}h). The nightly job has failed or has not run. Log: ~freeinference/backup.log"
    else
        clear_alert "backup_stale"
    fi

    # A step change, not a verdict: archiving api_logs rows out legitimately
    # shrinks the dump, and a truncated one is already rejected at write time by
    # backup.sh's sentinel check. Warn, name the likely cause, and let it go
    # quiet once the new size is the norm.
    if [[ -n "$prev_size" ]] && ((prev_size > 0)) && ((newest_size * 100 < prev_size * MIN_SIZE_PCT)); then
        send_alert "backup_small" warning \
            "DB backup SHRANK SHARPLY: ${newest_key} is $((newest_size / GIB)) GiB, $((newest_size * 100 / prev_size))% of the previous backup ($((prev_size / GIB)) GiB, threshold ${MIN_SIZE_PCT}%). Expected if api_logs rows were just archived out; otherwise check what happened to the data."
    else
        clear_alert "backup_small"
    fi

    if ((PROBLEMS == 0)); then
        log_success "Backup looks healthy: ${newest_key}, ${age_hours}h old, $((newest_size / GIB)) GiB"
    fi
}

check_disk() {
    log_info "Checking free space on ${MOUNT} ..."

    local avail_bytes avail_gib pct_used
    # -P for portable single-line output, -B1 for bytes so no unit parsing.
    avail_bytes=$(df -PB1 "$MOUNT" 2> /dev/null | awk 'NR==2 {print $4}') || true
    if [[ -z "$avail_bytes" || ! "$avail_bytes" =~ ^[0-9]+$ ]]; then
        send_alert "disk_unreadable" critical \
            "Disk check FAILED: could not read free space for ${MOUNT}."
        return 0
    fi
    avail_gib=$((avail_bytes / GIB))
    pct_used=$(df -P "$MOUNT" 2> /dev/null | awk 'NR==2 {print $5}') || pct_used="?"

    log_info "  available: ${avail_gib} GiB (${pct_used} used, alert below ${MIN_FREE_GIB} GiB)"

    if ((avail_gib < MIN_FREE_GIB)); then
        send_alert "disk_low" critical \
            "DISK LOW on ${MOUNT}: ${avail_gib} GiB available (${pct_used} used, threshold ${MIN_FREE_GIB} GiB). Postgres and the nightly dump share this volume; a full volume can take the database down."
    else
        clear_alert "disk_low"
        log_success "Free space OK: ${avail_gib} GiB available on ${MOUNT}"
    fi
}

# Parse command line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --s3-bucket)
                S3_URI="${2%/}"
                shift 2
                ;;
            --max-age-hours)
                MAX_AGE_HOURS="$2"
                shift 2
                ;;
            --min-free-gib)
                MIN_FREE_GIB="$2"
                shift 2
                ;;
            --min-size-pct)
                MIN_SIZE_PCT="$2"
                shift 2
                ;;
            --mount)
                MOUNT="$2"
                shift 2
                ;;
            --cooldown-sec)
                COOLDOWN_SEC="$2"
                shift 2
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --test)
                TEST_ONLY=true
                shift
                ;;
            --help | -h)
                show_help
                ;;
            *)
                log_error "Unknown option: $1"
                log_error "Run with --help for usage."
                exit 1
                ;;
        esac
    done
}

main() {
    parse_args "$@"

    if [[ "$TEST_ONLY" == true ]]; then
        # Bypasses the cooldown on purpose: a test that silently does nothing
        # because it ran twice would defeat its only job.
        COOLDOWN_SEC=0
        send_alert "monitor_test" warning \
            "hybridInference backup/disk monitor test alert from ${HOSTNAME:-unknown} — if you can read this, the alert path works."
        clear_alert "monitor_test"
        exit 0
    fi

    for cmd in aws curl; do
        command -v "$cmd" &> /dev/null || log_warning "${cmd} not found; some checks will degrade"
    done

    check_backup
    check_disk

    if ((PROBLEMS > 0)); then
        log_error "${PROBLEMS} problem(s) found"
        exit 1
    fi
    log_success "All checks passed"
}

main "$@"
