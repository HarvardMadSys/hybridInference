"""Tests for the backup and free-space monitor.

This script exists because backup.sh cannot report the two failures that
actually went unnoticed for days: a run that never fired at all, and a run that
"succeeded" while writing a fraction of the data. Both look identical to a quiet
success from inside the job, so the cases worth asserting here are the ones
where nothing appears to be wrong.

`aws`, `curl` and `df` are stubbed onto PATH — the bucket is a text listing and
the filesystem is a synthetic `df` table — so what is asserted is the alert the
script actually posts for a given state of the world.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "ops/db/check-backup-health.sh"

GIB = 1024**3
# Roughly the size a healthy dump of this database compresses to.
FULL_SIZE = 138 * GIB

AWS_STUB = """#!/bin/sh
printf 'aws %s\\n' "$*" >> "$CMD_LOG"
case "$1 $2" in
  "s3 ls")
    if [ -n "${FAKE_S3_FAIL:-}" ]; then
      printf '%s\\n' "${FAKE_S3_FAIL}" >&2
      exit 1
    fi
    [ -f "${FAKE_S3_LISTING:-/nonexistent}" ] && cat "$FAKE_S3_LISTING"
    exit 0
    ;;
esac
exit 0
"""

# Field order matches real `df -P`: the script reads $4 for available bytes and
# $5 for the capacity percentage, so those two positions are what matter.
DF_STUB = """#!/bin/sh
printf 'df %s\\n' "$*" >> "$CMD_LOG"
echo "Filesystem 1B-blocks Used Available Capacity Mounted-on"
echo "/dev/sda2 983349751808 616000000000 ${FAKE_AVAIL_BYTES} ${FAKE_PCT:-71%} /"
"""

CURL_STUB = """#!/bin/sh
printf 'curl %s\\n' "$*" >> "$CMD_LOG"
while [ $# -gt 0 ]; do
  case "$1" in
    -d) printf '%s\\n' "$2" >> "$WEBHOOK_PAYLOADS"; shift 2 ;;
    *) shift ;;
  esac
done
printf '%s' "${FAKE_CURL_CODE:-200}"
exit "${FAKE_CURL_EXIT:-0}"
"""


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_executable(bin_dir / "aws", AWS_STUB)
    _write_executable(bin_dir / "df", DF_STUB)
    _write_executable(bin_dir / "curl", CURL_STUB)
    return bin_dir


def _object_line(*, hours_ago: float, size: int, stamp: str | None = None) -> str:
    """One `aws s3 ls` row for a backup uploaded `hours_ago`."""
    when = datetime.now(UTC) - timedelta(hours=hours_ago)
    key_stamp = stamp or when.strftime("%Y%m%d_%H%M%S")
    return f"{when:%Y-%m-%d %H:%M:%S} {size} freeinference_db_{key_stamp}.sql.zst"


class Run:
    """One invocation. Cooldown tests run the script repeatedly against a shared
    state directory, so each run records to its own files — otherwise a later
    run inherits the earlier run's payloads and every suppression assertion
    passes or fails for the wrong reason.
    """

    def __init__(
        self, proc: subprocess.CompletedProcess[str], payload_file: Path, cmd_log: Path
    ) -> None:
        self.proc = proc
        self.payload_file = payload_file
        self.cmd_log = cmd_log

    @property
    def returncode(self) -> int:
        return self.proc.returncode

    @property
    def output(self) -> str:
        return self.proc.stdout + self.proc.stderr

    @property
    def alerts(self) -> list[str]:
        """The `text` of every payload this run POSTed, in order."""
        if not self.payload_file.exists():
            return []
        return [
            json.loads(line)["text"]
            for line in self.payload_file.read_text().splitlines()
            if line.strip()
        ]

    def commands(self, needle: str) -> list[str]:
        if not self.cmd_log.exists():
            return []
        return [c for c in self.cmd_log.read_text().splitlines() if needle in c]


def _run(
    tmp_path: Path,
    *args: str,
    objects: list[str] | None = None,
    avail_gib: int = 253,
    state_dir: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> Run:
    seq = len(list(tmp_path.glob("payloads-*.jsonl")))
    payload_file = tmp_path / f"payloads-{seq}.jsonl"
    cmd_log = tmp_path / f"commands-{seq}.log"

    listing_file = tmp_path / "s3-listing.txt"
    if objects is None:
        objects = [_object_line(hours_ago=2, size=FULL_SIZE)]
    listing_file.write_text("".join(f"{line}\n" for line in objects))

    env = {
        **os.environ,
        "PATH": f"{_stub_bin(tmp_path)}:{os.environ['PATH']}",
        "CMD_LOG": str(cmd_log),
        "FAKE_S3_LISTING": str(listing_file),
        "FAKE_AVAIL_BYTES": str(avail_gib * GIB),
        "WEBHOOK_PAYLOADS": str(payload_file),
        "MONITOR_ALERT_WEBHOOK_URL": "https://hooks.slack.test/monitor",
        "MONITOR_STATE_DIR": str(state_dir or tmp_path / "state"),
        "MONITOR_ENV_FILE": str(tmp_path / "absent.env"),
        **(env_overrides or {}),
    }

    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return Run(proc, payload_file, cmd_log)


# ── The healthy case ─────────────────────────────────────────────────────


def test_a_recent_full_backup_and_free_space_alerts_nothing(tmp_path: Path) -> None:
    run = _run(tmp_path)

    assert run.returncode == 0, run.output
    assert run.alerts == []


# ── A backup that never arrived ──────────────────────────────────────────


def test_a_backup_older_than_the_threshold_alerts(tmp_path: Path) -> None:
    """The case cron and notify_failure are both blind to: the job never ran."""
    run = _run(tmp_path, objects=[_object_line(hours_ago=44, size=FULL_SIZE)])

    assert run.returncode != 0
    assert len(run.alerts) == 1
    assert "STALE" in run.alerts[0]
    assert "44h old" in run.alerts[0]


def test_a_backup_still_inside_the_grace_window_does_not_alert(tmp_path: Path) -> None:
    """A dump that runs long must not page anyone at 04:20 while it is working."""
    run = _run(tmp_path, objects=[_object_line(hours_ago=25, size=FULL_SIZE)])

    assert run.returncode == 0, run.output
    assert run.alerts == []


def test_an_empty_bucket_alerts(tmp_path: Path) -> None:
    run = _run(tmp_path, objects=[])

    assert run.returncode != 0
    assert "no backup objects" in run.alerts[0]


def test_a_bucket_that_cannot_be_listed_alerts_rather_than_passing(tmp_path: Path) -> None:
    """Expired credentials must not read as a healthy quiet run."""
    run = _run(tmp_path, env_overrides={"FAKE_S3_FAIL": "An error occurred (AccessDenied)"})

    assert run.returncode != 0
    assert "Backup state is unknown" in run.alerts[0]


def test_a_partial_upload_does_not_count_as_a_backup(tmp_path: Path) -> None:
    """A discarded .partial key is not restorable, so it must not satisfy the check."""
    when = datetime.now(UTC) - timedelta(hours=2)
    run = _run(
        tmp_path,
        objects=[
            f"{when:%Y-%m-%d %H:%M:%S} {FULL_SIZE} "
            f"freeinference_db_{when:%Y%m%d_%H%M%S}.sql.zst.partial",
            _object_line(hours_ago=44, size=FULL_SIZE),
        ],
    )

    assert run.returncode != 0
    assert "STALE" in run.alerts[0]


# ── A backup that arrived but is not a backup ────────────────────────────


def test_a_sharp_shrink_against_the_previous_backup_warns(tmp_path: Path) -> None:
    """4 GiB landing where 129 GiB did is worth one look, either way."""
    run = _run(
        tmp_path,
        objects=[
            _object_line(hours_ago=26, size=FULL_SIZE),
            _object_line(hours_ago=2, size=4 * GIB),
        ],
    )

    assert run.returncode != 0
    assert len(run.alerts) == 1
    assert "SHRANK SHARPLY" in run.alerts[0]
    assert ":warning:" in run.alerts[0], "a step change is not a page"


def test_a_deliberate_archival_goes_quiet_once_the_new_size_is_the_norm(tmp_path: Path) -> None:
    """The regression this replaced: a largest-object baseline never recovers.

    Archiving api_logs rows out took the real dump from 129 GiB to 4 GiB on
    2026-08-08, and GFS retention keeps the pre-archival copies for weeks as the
    weekly and monthly. Measured against the largest object present, every
    healthy backup after that reads as a critical failure until they age out,
    which is how a monitor teaches its readers to ignore it.
    """
    run = _run(
        tmp_path,
        objects=[
            _object_line(hours_ago=99, size=FULL_SIZE),  # monthly, still retained
            _object_line(hours_ago=75, size=FULL_SIZE),  # weekly, still retained
            _object_line(hours_ago=26, size=4 * GIB),  # the archival happened here
            _object_line(hours_ago=2, size=4 * GIB),  # steady at the new size
        ],
    )

    assert run.returncode == 0, run.output
    assert run.alerts == [], "a settled post-archival size must not keep alerting"


def test_ordinary_growth_is_not_mistaken_for_a_shrink(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        objects=[
            _object_line(hours_ago=26, size=FULL_SIZE),
            _object_line(hours_ago=2, size=FULL_SIZE + 3 * GIB),
        ],
    )

    assert run.returncode == 0, run.output
    assert run.alerts == []


def test_a_lone_first_backup_has_nothing_to_compare_against(tmp_path: Path) -> None:
    """A fresh bucket must not read as a shrink from zero."""
    run = _run(tmp_path, objects=[_object_line(hours_ago=2, size=4 * GIB)])

    assert run.returncode == 0, run.output
    assert run.alerts == []
    assert "no previous backup" in run.output


# ── Free space ───────────────────────────────────────────────────────────


def test_free_space_below_the_threshold_alerts(tmp_path: Path) -> None:
    run = _run(tmp_path, avail_gib=61)

    assert run.returncode != 0
    assert len(run.alerts) == 1
    assert "DISK LOW" in run.alerts[0]
    assert "61 GiB available" in run.alerts[0]


def test_free_space_at_the_threshold_does_not_alert(tmp_path: Path) -> None:
    """80 GiB available is the boundary the operator asked for, not a breach."""
    run = _run(tmp_path, avail_gib=80)

    assert run.returncode == 0, run.output
    assert run.alerts == []


def test_the_free_space_threshold_is_configurable(tmp_path: Path) -> None:
    run = _run(tmp_path, "--min-free-gib", "300", avail_gib=253)

    assert run.returncode != 0
    assert "DISK LOW" in run.alerts[0]


def test_both_conditions_alert_independently(tmp_path: Path) -> None:
    run = _run(tmp_path, objects=[_object_line(hours_ago=44, size=FULL_SIZE)], avail_gib=10)

    assert run.returncode != 0
    assert len(run.alerts) == 2
    assert any("STALE" in a for a in run.alerts)
    assert any("DISK LOW" in a for a in run.alerts)


# ── Alert delivery ───────────────────────────────────────────────────────


def test_a_repeat_of_the_same_condition_is_suppressed(tmp_path: Path) -> None:
    """An hourly timer must not turn one problem into 24 messages a day."""
    state = tmp_path / "state"
    stale = [_object_line(hours_ago=44, size=FULL_SIZE)]

    first = _run(tmp_path, objects=stale, state_dir=state)
    assert len(first.alerts) == 1

    second = _run(tmp_path, objects=stale, state_dir=state)
    assert second.returncode != 0, "the condition is still a failure while suppressed"
    assert second.alerts == [], "the same alert must not be re-sent inside the cooldown"
    assert "cooldown" in second.output


def test_recovery_clears_the_cooldown_so_the_next_failure_alerts_at_once(tmp_path: Path) -> None:
    state = tmp_path / "state"

    _run(tmp_path, objects=[_object_line(hours_ago=44, size=FULL_SIZE)], state_dir=state)
    healthy = _run(tmp_path, state_dir=state)
    assert healthy.returncode == 0, healthy.output

    again = _run(tmp_path, objects=[_object_line(hours_ago=44, size=FULL_SIZE)], state_dir=state)
    assert len(again.alerts) == 1, "a recovered-then-failed condition must alert again"


def test_a_rejected_post_is_not_recorded_as_delivered(tmp_path: Path) -> None:
    """A revoked webhook has to keep retrying, not go quiet after one attempt."""
    state = tmp_path / "state"
    stale = [_object_line(hours_ago=44, size=FULL_SIZE)]

    first = _run(
        tmp_path,
        objects=stale,
        state_dir=state,
        env_overrides={"FAKE_CURL_EXIT": "22", "FAKE_CURL_CODE": "403"},
    )
    assert "unreported" in first.output

    second = _run(tmp_path, objects=stale, state_dir=state)
    assert len(second.alerts) == 1, "a failed delivery must not start a cooldown"


def test_a_dry_run_reports_without_posting(tmp_path: Path) -> None:
    run = _run(tmp_path, "--dry-run", objects=[_object_line(hours_ago=44, size=FULL_SIZE)])

    assert run.returncode != 0
    assert run.alerts == []
    assert not run.commands("curl")


def test_the_test_flag_proves_the_alert_path_end_to_end(tmp_path: Path) -> None:
    run = _run(tmp_path, "--test")

    assert run.returncode == 0, run.output
    assert len(run.alerts) == 1
    assert "test alert" in run.alerts[0]


def test_a_test_alert_is_never_swallowed_by_a_cooldown(tmp_path: Path) -> None:
    """A verification step that silently does nothing on the second run is useless."""
    state = tmp_path / "state"

    assert len(_run(tmp_path, "--test", state_dir=state).alerts) == 1
    assert len(_run(tmp_path, "--test", state_dir=state).alerts) == 1


def test_no_webhook_configured_degrades_instead_of_crashing(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        objects=[_object_line(hours_ago=44, size=FULL_SIZE)],
        env_overrides={"MONITOR_ALERT_WEBHOOK_URL": ""},
    )

    assert run.returncode != 0, "the problem is still reported through the exit status"
    assert "no webhook configured" in run.output
    assert not run.commands("curl")


def test_an_env_file_without_a_slack_key_warns_instead_of_aborting(tmp_path: Path) -> None:
    """The keyless-but-readable .env is the path a rotated-out secret leaves behind.

    Under `set -e` this is one bad return away from swallowing the alert *and*
    the warning about not sending it, which is the worst of both.
    """
    env_file = tmp_path / "keyless.env"
    env_file.write_text("DB_NAME=x\nUNRELATED=1\n")

    run = _run(
        tmp_path,
        objects=[_object_line(hours_ago=44, size=FULL_SIZE)],
        env_overrides={"MONITOR_ALERT_WEBHOOK_URL": "", "MONITOR_ENV_FILE": str(env_file)},
    )

    assert run.returncode != 0
    assert "STALE" in run.output, "the condition itself must still be reported to the log"
    assert "no webhook configured" in run.output
    assert not run.commands("curl")


def test_the_webhook_is_read_from_the_env_file_when_unset(tmp_path: Path) -> None:
    """Same fallback backup.sh uses, so the monitor needs no secret of its own."""
    env_file = tmp_path / "deployment.env"
    env_file.write_text('DB_NAME=x\nSLACK_WEBHOOK_URL="https://hooks.slack.test/from-env"\n')

    run = _run(
        tmp_path,
        objects=[_object_line(hours_ago=44, size=FULL_SIZE)],
        env_overrides={"MONITOR_ALERT_WEBHOOK_URL": "", "MONITOR_ENV_FILE": str(env_file)},
    )

    assert "https://hooks.slack.test/from-env" in run.commands("curl")[0]


def test_the_webhook_url_never_appears_in_the_output(tmp_path: Path) -> None:
    """The log lands in a world-readable file under ~freeinference."""
    run = _run(
        tmp_path,
        objects=[_object_line(hours_ago=44, size=FULL_SIZE)],
        env_overrides={"MONITOR_ALERT_WEBHOOK_URL": "https://hooks.slack.test/T0SECRET/B0SECRET"},
    )

    assert "T0SECRET" not in run.output
