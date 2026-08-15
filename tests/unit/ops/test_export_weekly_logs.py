"""Tests for the weekly api_logs export + gated prune script.

The interesting contract is the order of operations, not the ssh wire format:
the remote archive has to verify before prune is even considered, and prune
must not run when the copy failed or the nightly backup is stale. Stubs stand
in for ssh, export_logs.py, zstd-on-the-remote, check-backup-health.sh and
archive-old-logs.sh.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "ops/db/export-weekly-logs.sh"

REMOTE = "exporter@research.example"
DEST_DIR = "/data/api-log-exports"
# A Saturday in the same week the default window is pinned against in
# test_export_logs.test_previous_iso_week_from_a_saturday.
WEEK_START = "2026-08-03"
WEEK_END = "2026-08-09"
WEEK_UNTIL = "2026-08-10"
REMOTE_NAME = f"api_logs_{WEEK_START}_{WEEK_END}.jsonl.zst"


SSH_STUB = r"""#!/bin/bash
printf 'ssh %s\n' "$*" >> "$CMD_LOG"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -p) shift 2 ;;
    -o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift  # host
cmd="$*"
# Strip a single layer of surrounding quotes the script puts around paths.
unquote() {
  local s="$1"
  s="${s#\'}"
  s="${s%\'}"
  printf '%s' "$s"
}

root="$FAKE_REMOTE"
map_path() {
  printf '%s%s' "$root" "$(unquote "$1")"
}

if [[ "$cmd" == mkdir\ -p* ]]; then
  mkdir -p "$(map_path "${cmd#mkdir -p }")"
  exit 0
fi
if [[ "$cmd" == rm\ -f* ]]; then
  rm -f "$(map_path "${cmd#rm -f }")"
  exit 0
fi
if [[ "$cmd" == cat\ \>* ]]; then
  dest="$(map_path "${cmd#cat > }")"
  mkdir -p "$(dirname "$dest")"
  cat > "$dest"
  exit "${FAKE_SSH_CAT_EXIT:-0}"
fi
if [[ "$cmd" == cat\ * ]]; then
  src="$(map_path "${cmd#cat }")"
  cat "$src"
  exit "${FAKE_SSH_CAT_EXIT:-0}"
fi
if [[ "$cmd" == test\ -f* ]]; then
  # test -f 'final' && zstd -q -t 'final'
  rest="${cmd#test -f }"
  final="${rest%% && *}"
  path="$(map_path "$final")"
  if [[ ! -f "$path" ]]; then
    exit 1
  fi
  if [[ "${FAKE_ZSTD_FAIL:-}" == "final" ]]; then
    exit 1
  fi
  exit 0
fi
if [[ "$cmd" == zstd\ -q\ -t* ]]; then
  # zstd -q -t 'partial' && mv -f 'partial' 'final'
  rest="${cmd#zstd -q -t }"
  partial="${rest%% && *}"
  path="$(map_path "$partial")"
  if [[ ! -s "$path" ]]; then
    exit 1
  fi
  if [[ "${FAKE_ZSTD_FAIL:-}" == "partial" ]]; then
    exit 1
  fi
  if [[ "$rest" == *" && mv -f "* ]]; then
    final="${rest#* && mv -f }"
    src="$(map_path "${final%% *}")"
    dst="$(map_path "${final#* }")"
    mv -f "$src" "$dst"
  fi
  exit 0
fi
if [[ "$cmd" == stat\ -c\ %s* ]]; then
  path="$(map_path "${cmd#stat -c %s }")"
  if [[ "${FAKE_STAT_ZERO:-}" == "1" ]]; then
    echo 0
    exit 0
  fi
  stat -c %s "$path"
  exit 0
fi
echo "unhandled ssh command: $cmd" >&2
exit 90
"""

PYTHON_STUB = r"""#!/bin/sh
printf 'python %s\n' "$*" >> "$CMD_LOG"
printf '%s\n' "${FAKE_EXPORT_BYTES:-zst-payload}"
echo "Exported 3 rows to stdout" >&2
exit "${FAKE_EXPORT_EXIT:-0}"
"""

BACKUP_STUB = r"""#!/bin/sh
printf 'backup-health %s\n' "$*" >> "$CMD_LOG"
exit "${FAKE_BACKUP_EXIT:-0}"
"""

ARCHIVE_STUB = r"""#!/bin/sh
printf 'archive-old-logs %s\n' "$*" >> "$CMD_LOG"
exit "${FAKE_PRUNE_EXIT:-0}"
"""

AWS_STUB = r"""#!/bin/sh
printf 'aws %s\n' "$*" >> "$CMD_LOG"
case "$1 $2" in
  "s3api head-object")
    key=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --key) key="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    obj="$S3_STORE/$(basename "$key")"
    if [ -f "$obj" ]; then
      wc -c < "$obj" | tr -d ' '
      exit 0
    fi
    exit 254
    ;;
  "s3 cp")
    dest="$4"
    mkdir -p "$S3_STORE"
    cat > "$S3_STORE/$(basename "$dest")"
    exit "${FAKE_S3_CP_EXIT:-0}"
    ;;
  "s3 mv")
    mv "$S3_STORE/$(basename "$3")" "$S3_STORE/$(basename "$4")"
    exit "${FAKE_S3_MV_EXIT:-0}"
    ;;
  "s3 rm")
    rm -f "$S3_STORE/$(basename "$3")"
    exit 0
    ;;
esac
exit 0
"""


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Run:
    def __init__(self, proc: subprocess.CompletedProcess[str], tmp_path: Path) -> None:
        self.proc = proc
        self._tmp = tmp_path

    @property
    def returncode(self) -> int:
        return self.proc.returncode

    @property
    def output(self) -> str:
        return self.proc.stdout + self.proc.stderr

    @property
    def commands(self) -> list[str]:
        log = self._tmp / "commands.log"
        return log.read_text().splitlines() if log.exists() else []

    def find(self, needle: str) -> list[str]:
        return [c for c in self.commands if needle in c]

    def remote_files(self) -> list[str]:
        root = self._tmp / "remote"
        return sorted(p.name for p in root.rglob("*") if p.is_file())

    def s3_files(self) -> list[str]:
        store = self._tmp / "s3"
        return sorted(p.name for p in store.iterdir()) if store.exists() else []


def _run(
    tmp_path: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
) -> Run:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_executable(bin_dir / "ssh", SSH_STUB)
    _write_executable(bin_dir / "python", PYTHON_STUB)
    _write_executable(bin_dir / "aws", AWS_STUB)
    _write_executable(tmp_path / "check-backup-health.sh", BACKUP_STUB)
    _write_executable(tmp_path / "archive-old-logs.sh", ARCHIVE_STUB)

    (tmp_path / "remote").mkdir(exist_ok=True)
    (tmp_path / "project").mkdir(exist_ok=True)
    (tmp_path / "s3").mkdir(exist_ok=True)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CMD_LOG": str(tmp_path / "commands.log"),
        "FAKE_REMOTE": str(tmp_path / "remote"),
        "S3_STORE": str(tmp_path / "s3"),
        "EXPORT_PROJECT_ROOT": str(tmp_path / "project"),
        "EXPORT_PYTHON": str(bin_dir / "python"),
        "EXPORT_SSH": str(bin_dir / "ssh"),
        "EXPORT_LOGS_PY": str(REPO_ROOT / "ops/db/export_logs.py"),
        "EXPORT_BACKUP_HEALTH_SCRIPT": str(tmp_path / "check-backup-health.sh"),
        "EXPORT_ARCHIVE_SCRIPT": str(tmp_path / "archive-old-logs.sh"),
        **(env_overrides or {}),
    }

    proc = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--since",
            WEEK_START,
            "--until",
            WEEK_UNTIL,
            "--remote",
            REMOTE,
            "--port",
            "10021",
            "--dest-dir",
            DEST_DIR,
            "--s3-data",
            "s3://bucket/weekly-jsonl",
            "--s3-archive",
            "s3://bucket/archive/api_logs",
            *args,
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return Run(proc, tmp_path)


def test_dry_run_writes_nothing_and_does_not_prune(tmp_path: Path) -> None:
    run = _run(tmp_path, "--dry-run")

    assert run.returncode == 0, run.output
    assert WEEK_START in run.output and WEEK_END in run.output
    assert "s3://bucket/weekly-jsonl" in run.output
    assert run.remote_files() == []
    assert run.s3_files() == []
    assert run.find("python ") == []
    assert run.find("aws ") == []
    assert run.find("archive-old-logs") == []


def test_copy_then_backup_check_then_prune(tmp_path: Path) -> None:
    run = _run(tmp_path)

    assert run.returncode == 0, run.output
    assert REMOTE_NAME in run.remote_files()
    assert f"{REMOTE_NAME}.partial" not in run.remote_files()
    assert REMOTE_NAME in run.s3_files()
    assert f"{REMOTE_NAME}.partial" not in run.s3_files()

    python = run.find("python ")
    assert python, run.commands
    assert f"--since {WEEK_START}" in python[0]
    assert f"--until {WEEK_UNTIL}" in python[0]
    assert "-o -" in python[0]

    ssh = run.find("ssh ")
    assert any(" -p 10021 " in c and REMOTE in c for c in ssh), ssh

    uploads = run.find("aws s3 cp -")
    assert len(uploads) == 1, uploads
    assert "--expected-size" in uploads[0]
    assert uploads[0].split()[4].endswith(".jsonl.zst.partial"), uploads[0]
    assert run.find("aws s3 mv"), run.commands

    health = run.find("backup-health ")
    prune = run.find("archive-old-logs ")
    assert health and prune, run.commands
    assert run.commands.index(uploads[0]) < run.commands.index(health[0])
    assert run.commands.index(health[0]) < run.commands.index(prune[0])
    assert "--retention-days 30" in prune[0]
    assert "--s3-archive s3://bucket/archive/api_logs" in prune[0]


def test_stale_backup_keeps_the_copy_and_does_not_prune(tmp_path: Path) -> None:
    run = _run(tmp_path, env_overrides={"FAKE_BACKUP_EXIT": "1"})

    assert run.returncode == 1, run.output
    assert REMOTE_NAME in run.remote_files()
    assert REMOTE_NAME in run.s3_files()
    assert run.find("archive-old-logs") == []
    assert "leaving rows in place" in run.output


def test_failed_export_does_not_prune(tmp_path: Path) -> None:
    run = _run(tmp_path, env_overrides={"FAKE_EXPORT_EXIT": "2"})

    assert run.returncode == 1, run.output
    assert REMOTE_NAME not in run.remote_files()
    assert run.find("aws s3 cp") == []
    assert run.find("backup-health") == []
    assert run.find("archive-old-logs") == []


def test_failed_s3_upload_keeps_the_copy_and_does_not_prune(tmp_path: Path) -> None:
    run = _run(tmp_path, env_overrides={"FAKE_S3_CP_EXIT": "1"})

    assert run.returncode == 1, run.output
    assert REMOTE_NAME in run.remote_files()
    assert run.find("backup-health") == []
    assert run.find("archive-old-logs") == []


def test_corrupt_remote_archive_does_not_prune(tmp_path: Path) -> None:
    run = _run(tmp_path, env_overrides={"FAKE_ZSTD_FAIL": "partial"})

    assert run.returncode == 1, run.output
    assert REMOTE_NAME not in run.remote_files()
    assert run.find("archive-old-logs") == []


def test_zero_byte_remote_archive_is_rejected(tmp_path: Path) -> None:
    run = _run(tmp_path, env_overrides={"FAKE_STAT_ZERO": "1"})

    assert run.returncode == 1, run.output
    assert "0 bytes" in run.output
    assert run.find("archive-old-logs") == []


def test_existing_verified_remote_file_skips_export_and_still_prunes(tmp_path: Path) -> None:
    dest = tmp_path / "remote" / DEST_DIR.lstrip("/")
    dest.mkdir(parents=True)
    (dest / REMOTE_NAME).write_bytes(b"already-there")

    run = _run(tmp_path)

    assert run.returncode == 0, run.output
    assert run.find("python ") == []
    assert run.find("aws s3 cp -")
    assert run.find("archive-old-logs")


def test_skip_prune_copies_only(tmp_path: Path) -> None:
    run = _run(tmp_path, "--skip-prune")

    assert run.returncode == 0, run.output
    assert REMOTE_NAME in run.remote_files()
    assert REMOTE_NAME in run.s3_files()
    assert run.find("backup-health") == []
    assert run.find("archive-old-logs") == []


def test_default_week_is_the_previous_complete_iso_week(tmp_path: Path) -> None:
    """Without --since/--until the script asks date(1) for last Sun-Sat UTC."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_executable(bin_dir / "ssh", SSH_STUB)
    _write_executable(bin_dir / "python", PYTHON_STUB)
    _write_executable(tmp_path / "check-backup-health.sh", BACKUP_STUB)
    _write_executable(tmp_path / "archive-old-logs.sh", ARCHIVE_STUB)
    (tmp_path / "remote").mkdir(exist_ok=True)
    (tmp_path / "project").mkdir(exist_ok=True)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CMD_LOG": str(tmp_path / "commands.log"),
        "FAKE_REMOTE": str(tmp_path / "remote"),
        "EXPORT_PROJECT_ROOT": str(tmp_path / "project"),
        "EXPORT_PYTHON": str(bin_dir / "python"),
        "EXPORT_SSH": str(bin_dir / "ssh"),
        "EXPORT_LOGS_PY": str(REPO_ROOT / "ops/db/export_logs.py"),
        "EXPORT_BACKUP_HEALTH_SCRIPT": str(tmp_path / "check-backup-health.sh"),
        "EXPORT_ARCHIVE_SCRIPT": str(tmp_path / "archive-old-logs.sh"),
    }
    proc = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--dry-run",
            "--remote",
            REMOTE,
            "--dest-dir",
            DEST_DIR,
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Window:" in (proc.stdout + proc.stderr)
