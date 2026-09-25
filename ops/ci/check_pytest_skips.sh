#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <skip-pattern> <pytest-log>" >&2
  exit 2
fi

pattern=$1
log_file=$2

if ! command -v grep >/dev/null 2>&1; then
  echo "::error::pytest skip checker requires grep, but grep is unavailable" >&2
  exit 2
fi

scan_rc=0
grep -Eni "$pattern" "$log_file" || scan_rc=$?
case "$scan_rc" in
  0)
    echo "::error::Unexpected PostgreSQL/database test skip detected" >&2
    exit 1
    ;;
  1)
    exit 0
    ;;
  *)
    echo "::error::pytest skip checker failed while scanning ${log_file}" >&2
    exit 2
    ;;
esac
