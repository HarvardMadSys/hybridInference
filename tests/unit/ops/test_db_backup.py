"""Tests for the PostgreSQL backup script.

The database is larger than the free space on the volume Postgres sits on, so
the dump is streamed `pg_dump | zstd | aws s3 cp -` and never materialised
locally. That makes the interesting cases the ones where a run *looks* fine and
is not: a truncated pipe still yields a valid zstd frame and a completed
multipart upload, so an object of plausible size can land holding half a
database. The nightly job then prunes older backups on top of it.

Each test runs the real script with `docker`, `aws`, `zstd` and `curl` stubbed
onto PATH — the stubs append to a command log and keep a directory that stands
in for the bucket — so what is asserted is the command line the script actually
issues and the order it issues it in, not a mocked-out reimplementation.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "ops/db/backup.sh"

# The line pg_dump writes when, and only when, it reached the end of the dump.
SENTINEL = "-- PostgreSQL database dump complete"

# The default floor the script refuses to promote below.
MIN_OBJECT_BYTES = 1024 * 1024

# What the installed crontab runs. It must keep working verbatim.
PROD_ARGS = (
    "--compress",
    "--s3-bucket",
    "s3://harvardsys-backup/freeinference",
    "--s3-only",
)

DOCKER_STUB = """#!/bin/sh
printf 'docker %s\\n' "$*" >> "$CMD_LOG"
case "$1" in
  ps)
    [ -n "${FAKE_CONTAINER:-}" ] && echo "$FAKE_CONTAINER"
    exit 0
    ;;
  exec)
    [ -f "$FAKE_DUMP" ] && cat "$FAKE_DUMP"
    exit "${FAKE_DUMP_EXIT:-0}"
    ;;
esac
exit 0
"""

# Stands in for the bucket with a directory. Object keys are flattened to their
# basename, which is enough: every key this script writes is unique by
# timestamp.
AWS_STUB = """#!/bin/sh
printf 'aws %s\\n' "$*" >> "$CMD_LOG"
case "$1 $2" in
  "sts get-caller-identity")
    exit "${FAKE_STS_EXIT:-0}"
    ;;
  "s3api head-bucket")
    exit "${FAKE_HEAD_BUCKET_EXIT:-0}"
    ;;
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
    echo "NoSuchKey" >&2
    exit 254
    ;;
esac
case "$1 $2" in
  "s3 cp")
    if [ "$3" = "-" ]; then
      cat > "$S3_STORE/$(basename "$4")"
    else
      cp "$3" "$S3_STORE/$(basename "$4")"
    fi
    exit "${FAKE_S3_CP_EXIT:-0}"
    ;;
  "s3 mv")
    mv "$S3_STORE/$(basename "$3")" "$S3_STORE/$(basename "$4")"
    exit 0
    ;;
  "s3 ls")
    [ -f "${FAKE_S3_LISTING:-/nonexistent}" ] && cat "$FAKE_S3_LISTING"
    exit 0
    ;;
  "s3 rm")
    printf '%s\\n' "$3" >> "$S3_RM_LOG"
    rm -f "$S3_STORE/$(basename "$3")"
    exit 0
    ;;
esac
exit 0
"""

# Pass-through: the tests assert on the bytes that reach the bucket, so real
# compression would only obscure them. The flags are still recorded.
ZSTD_STUB = """#!/bin/sh
printf 'zstd %s\\n' "$*" >> "$CMD_LOG"
exec cat
"""

CURL_STUB = """#!/bin/sh
printf 'curl %s\\n' "$*" >> "$CMD_LOG"
while [ $# -gt 0 ]; do
  case "$1" in
    -d) printf '%s' "$2" > "$WEBHOOK_PAYLOAD"; shift 2 ;;
    *) shift ;;
  esac
done
exit 0
"""


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_executable(bin_dir / "docker", DOCKER_STUB)
    _write_executable(bin_dir / "aws", AWS_STUB)
    _write_executable(bin_dir / "zstd", ZSTD_STUB)
    _write_executable(bin_dir / "curl", CURL_STUB)
    return bin_dir


def _dump_text(*, complete: bool = True, padding: int = 0) -> str:
    """A stand-in for pg_dump's output, optionally cut off before the end.

    `padding` inflates it past the size floor the script enforces, so the
    default success path exercises that check for real rather than relaxing it.
    """
    body = [
        "--",
        "-- PostgreSQL database dump",
        "--",
        "CREATE DATABASE freeinference;",
        "COPY api_logs (id, prompt) FROM stdin;",
    ]
    if padding:
        body.append("-- " + "x" * padding)
    if complete:
        body += ["--", SENTINEL, "--"]
    return "\n".join(body) + "\n"


class Run:
    """One invocation of the script, plus the traces the stubs left behind."""

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

    @property
    def removed_keys(self) -> list[str]:
        log = self._tmp / "s3-rm.log"
        return log.read_text().splitlines() if log.exists() else []

    @property
    def bucket(self) -> list[str]:
        return sorted(p.name for p in (self._tmp / "s3").iterdir())

    def object_text(self, name: str) -> str:
        return (self._tmp / "s3" / name).read_text()

    def find(self, needle: str) -> list[str]:
        return [c for c in self.commands if needle in c]

    def index_of(self, needle: str) -> int:
        for i, command in enumerate(self.commands):
            if needle in command:
                return i
        raise AssertionError(f"{needle!r} never ran; log was:\n" + "\n".join(self.commands))


def _run(
    tmp_path: Path,
    *args: str,
    dump: str | None = None,
    listing: str | None = None,
    env_overrides: dict[str, str] | None = None,
    env_file_extra: str = "",
) -> Run:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / ".env").write_text(
        "DB_NAME=freeinference\nDB_USER=fi\nDB_PASSWORD=s3cret\n" + env_file_extra
    )
    (tmp_path / "s3").mkdir(exist_ok=True)

    dump_file = tmp_path / "pg_dump.out"
    dump_file.write_text(dump if dump is not None else _dump_text(padding=2 * MIN_OBJECT_BYTES))

    listing_file = tmp_path / "s3-listing.txt"
    listing_file.write_text(listing or "")

    env = {
        **os.environ,
        "PATH": f"{_stub_bin(tmp_path)}:{os.environ['PATH']}",
        "BACKUP_PROJECT_ROOT": str(project),
        "POSTGRES_CONTAINER": "hybridinference-postgres",
        "CMD_LOG": str(tmp_path / "commands.log"),
        "S3_STORE": str(tmp_path / "s3"),
        "S3_RM_LOG": str(tmp_path / "s3-rm.log"),
        "FAKE_CONTAINER": "hybridinference-postgres",
        "FAKE_DUMP": str(dump_file),
        "FAKE_S3_LISTING": str(listing_file),
        "WEBHOOK_PAYLOAD": str(tmp_path / "webhook.json"),
        **(env_overrides or {}),
    }
    env.pop("BACKUP_ALERT_WEBHOOK_URL", None)
    env.update(env_overrides or {})

    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return Run(proc, tmp_path)


def _local_dumps(tmp_path: Path) -> list[Path]:
    """Every dump-shaped file anywhere under the project root."""
    project = tmp_path / "project"
    return sorted(p for p in project.rglob("*") if p.is_file() and ".sql" in p.name)


def _listing(*names: str) -> str:
    return "".join(f"2026-08-07 04:00:01  123456789 {name}\n" for name in names)


def _nightly_keys(days: range | list[int]) -> list[str]:
    """Object names for nights in January 2026."""
    return [f"freeinference_202601{day:02d}_040001.sql.zst" for day in days]


# ── Streaming: the path the nightly cron takes ────────────────────────────


def test_the_dump_is_streamed_to_s3_and_never_written_to_local_disk(tmp_path: Path) -> None:
    """The whole point: 120GB of dump cannot land on a volume with 166GB free.

    Run with the exact flags the installed crontab passes, including the now
    redundant --s3-only, which has to keep being accepted.
    """
    run = _run(tmp_path, *PROD_ARGS)

    assert run.returncode == 0, run.output

    uploads = run.find("aws s3 cp -")
    assert len(uploads) == 1, f"expected one streamed upload, got {uploads}"
    assert "s3://harvardsys-backup/freeinference/freeinference_" in uploads[0]

    assert not _local_dumps(tmp_path), (
        f"the dump was materialised locally: {_local_dumps(tmp_path)}"
    )
    assert not run.find("aws s3 cp /"), "nothing should be uploaded from a local file"


def test_the_upload_declares_its_size_so_it_does_not_die_at_80gb(tmp_path: Path) -> None:
    """`aws s3 cp -` assumes 8MB x 10000 parts unless told otherwise.

    That ceiling is 80GB. The dump is ~120GB, so without --expected-size the
    upload fails hours in, which is the least useful moment to find out.
    """
    run = _run(tmp_path, *PROD_ARGS)

    assert run.returncode == 0, run.output
    upload = run.find("aws s3 cp -")[0]
    fields = shlex.split(upload)
    assert "--expected-size" in fields, upload
    declared = int(fields[fields.index("--expected-size") + 1])
    assert declared > 120 * 1000**3, f"--expected-size {declared} is below the real dump size"


def test_the_object_is_promoted_only_after_it_verifies(tmp_path: Path) -> None:
    """Upload to .partial, check, then rename — so a final key is always good."""
    run = _run(tmp_path, *PROD_ARGS)

    assert run.returncode == 0, run.output
    assert run.find("aws s3 cp -")[0].split()[4].endswith(".sql.zst.partial")

    move = run.find("aws s3 mv")
    assert len(move) == 1, move
    source, destination = shlex.split(move[0])[3:5]
    assert source.endswith(".partial")
    assert destination == source[: -len(".partial")]

    assert run.index_of("s3api head-object") < run.index_of("aws s3 mv"), (
        "the size check has to happen while the object is still .partial"
    )
    assert run.bucket == [Path(destination).name]
    assert run.object_text(Path(destination).name).endswith(SENTINEL + "\n--\n")


def test_a_truncated_dump_is_rejected_and_nothing_is_pruned(tmp_path: Path) -> None:
    """The failure that looks like success.

    A pipe cut short still produces a valid zstd frame, and `aws s3 cp -` still
    completes the multipart upload, so the object lands at a plausible size.
    Only pg_dump's own end-of-dump marker distinguishes it — and getting this
    wrong means retention deletes good backups to make room for a broken one.
    """
    old = "freeinference_20260101_040001.sql.zst"
    run = _run(
        tmp_path,
        *PROD_ARGS,
        dump=_dump_text(complete=False, padding=2 * MIN_OBJECT_BYTES),
        listing=_listing(old),
    )

    assert run.returncode != 0
    assert "truncated" in run.output
    assert not run.find("aws s3 mv"), "a truncated dump was promoted to the real key"
    assert old not in run.removed_keys, "retention ran on top of a failed backup"
    assert not any(name.endswith(".sql.zst") for name in run.bucket), run.bucket


def test_an_implausibly_small_object_is_rejected(tmp_path: Path) -> None:
    """A complete-looking dump of a 518GB database cannot be a few hundred bytes.

    Whatever produced it — a pointed-at-the-wrong-database dump, an upload that
    wrote only its first part — it is not this backup.
    """
    run = _run(tmp_path, *PROD_ARGS, dump=_dump_text())

    assert run.returncode != 0
    assert "refusing to promote" in run.output
    assert not run.find("aws s3 mv")
    assert run.bucket == [], "the undersized object was left in the bucket"


def test_a_failing_pipeline_stage_fails_the_run(tmp_path: Path) -> None:
    """pg_dump dying mid-dump must not be masked by a happy `aws` at the end."""
    run = _run(tmp_path, *PROD_ARGS, env_overrides={"FAKE_DUMP_EXIT": "2"})

    assert run.returncode != 0
    assert "pg_dump exited 2" in run.output
    assert not run.find("aws s3 mv")


def test_a_failed_upload_takes_its_partial_object_with_it(tmp_path: Path) -> None:
    """A .partial left in the bucket is up to 120GB of storage nobody is billing for."""
    run = _run(tmp_path, *PROD_ARGS, env_overrides={"FAKE_S3_CP_EXIT": "1"})

    assert run.returncode != 0
    assert "aws exited 1" in run.output
    assert any(key.endswith(".partial") for key in run.removed_keys), run.removed_keys
    assert run.bucket == []


# ── Preflight: fail in seconds, not after a multi-hour dump ───────────────


def test_unusable_credentials_are_caught_before_the_dump_starts(tmp_path: Path) -> None:
    """The first real failure of this script burned a full dump, then found out.

    Under cron the CLI reads the invoking user's ~/.aws, which is exactly the
    thing that is wrong when nobody notices for four nights.
    """
    run = _run(tmp_path, *PROD_ARGS, env_overrides={"FAKE_STS_EXIT": "255"})

    assert run.returncode != 0
    assert "AWS credentials are unusable" in run.output
    assert not run.find("docker exec"), "it dumped the database before checking it could upload"


def test_a_bucket_that_does_not_exist_is_caught_before_the_dump_starts(tmp_path: Path) -> None:
    """The repo shipped `s3://freeinference/backup` — a bucket nobody owns."""
    run = _run(tmp_path, *PROD_ARGS, env_overrides={"FAKE_HEAD_BUCKET_EXIT": "255"})

    assert run.returncode != 0
    assert "harvardsys-backup" in run.output
    assert "not reachable" in run.output
    assert not run.find("docker exec")


def test_a_stopped_postgres_container_is_caught_before_anything_else(tmp_path: Path) -> None:
    run = _run(tmp_path, *PROD_ARGS, env_overrides={"FAKE_CONTAINER": "some-other-container"})

    assert run.returncode != 0
    assert "is not running" in run.output
    assert not run.find("docker exec")


# ── Failure notification ─────────────────────────────────────────────────


def test_a_failure_is_posted_to_the_alert_webhook(tmp_path: Path) -> None:
    """Four nights failed in a row with nobody watching. Something has to shout."""
    run = _run(
        tmp_path,
        *PROD_ARGS,
        dump=_dump_text(complete=False, padding=2 * MIN_OBJECT_BYTES),
        env_overrides={"BACKUP_ALERT_WEBHOOK_URL": "https://hooks.example.com/backup"},
    )

    assert run.returncode != 0
    payload = (tmp_path / "webhook.json").read_text()
    assert '"status":"failed"' in payload
    assert "truncated" in payload
    assert "https://hooks.example.com/backup" in run.find("curl")[0]


def test_no_webhook_configured_is_not_itself_a_failure(tmp_path: Path) -> None:
    """The alert is optional; a run without one must behave exactly as before."""
    run = _run(tmp_path, *PROD_ARGS)

    assert run.returncode == 0, run.output
    assert not run.find("curl")
    assert not (tmp_path / "webhook.json").exists()


def test_a_failure_alert_falls_back_to_the_deployment_slack_webhook(tmp_path: Path) -> None:
    """The env var the cron never exported must not be the only way to alert.

    /etc/cron.d/freeinference-backup is installed by hand and drifted from
    ops/db/backup-cron, so the MAILTO and BACKUP_ALERT_WEBHOOK_URL the template
    grew after the last silent outage were never live and five more nights
    failed unnoticed. Resolving the webhook from the deployed .env instead means
    the alert path survives that drift.
    """
    run = _run(
        tmp_path,
        *PROD_ARGS,
        dump=_dump_text(complete=False, padding=2 * MIN_OBJECT_BYTES),
        env_file_extra='SLACK_WEBHOOK_URL="https://hooks.slack.test/fallback"\n',
    )

    assert run.returncode != 0
    assert "https://hooks.slack.test/fallback" in run.find("curl")[0], run.output
    assert '"status":"failed"' in (tmp_path / "webhook.json").read_text()


def test_an_explicit_webhook_wins_over_the_env_file(tmp_path: Path) -> None:
    """An operator override has to beat the deployment default, not race it."""
    run = _run(
        tmp_path,
        *PROD_ARGS,
        dump=_dump_text(complete=False, padding=2 * MIN_OBJECT_BYTES),
        env_overrides={"BACKUP_ALERT_WEBHOOK_URL": "https://hooks.example.com/explicit"},
        env_file_extra='SLACK_WEBHOOK_URL="https://hooks.slack.test/fallback"\n',
    )

    curl = run.find("curl")[0]
    assert "https://hooks.example.com/explicit" in curl
    assert "fallback" not in curl


def test_the_quotes_around_an_env_file_value_are_not_sent_as_part_of_the_url(
    tmp_path: Path,
) -> None:
    """.env values are conventionally quoted; curl would POST to a 404 with them."""
    run = _run(
        tmp_path,
        *PROD_ARGS,
        dump=_dump_text(complete=False, padding=2 * MIN_OBJECT_BYTES),
        env_file_extra='SLACK_WEBHOOK_URL="https://hooks.slack.test/quoted"\n',
    )

    assert '"https://hooks.slack.test/quoted"' not in run.find("curl")[0]
    assert "https://hooks.slack.test/quoted" in run.find("curl")[0]


# ── Retention ────────────────────────────────────────────────────────────


def test_retention_skips_names_it_cannot_parse_and_still_prunes(tmp_path: Path) -> None:
    """`grep -oP ... | head -1` exits 1 on no match, and `set -e` did the rest.

    The `[[ -z ]] && continue` guard below it was unreachable, so one unrelated
    object in the prefix aborted the run *after* the upload — leaving retention
    unapplied and the script reporting failure on a good backup. PR #547 fixed
    the same class of bug here once already.
    """
    listing = _listing(
        "README.txt",
        "notes-without-a-date.sql.zst",
        *_nightly_keys(range(1, 11)),
    )
    run = _run(tmp_path, *PROD_ARGS, listing=listing)

    assert run.returncode == 0, run.output
    assert "README.txt" in run.output

    # 10 consecutive nights under 3/2/1: the three newest, plus one from the
    # ISO week before them. The rest go.
    assert set(run.removed_keys) == {
        f"s3://harvardsys-backup/freeinference/{name}" for name in _nightly_keys([1, 2, 3, 5, 6, 7])
    }, "the parseable, out-of-policy backups should still have been pruned"
    assert not any("README" in key or "notes" in key for key in run.removed_keys)


def test_retention_never_deletes_objects_this_script_did_not_write(tmp_path: Path) -> None:
    """The pruned prefix is shared with other tooling.

    ``s3://harvardsys-backup/freeinference`` also holds api_logs CSV archives and
    ad-hoc exports, and those carry a YYYYMMDD_HHMMSS stamp too. Matching on the
    timestamp alone would hand them to ``aws s3 rm`` the moment their date fell
    outside the GFS keep set — deleting someone else's data as a side effect of
    a database backup.
    """
    foreign = [
        "api_logs_20260105_000000.csv.zst",
        "runtime_overrides_20260106_000000.json",
        "freeinference_20260107_000000.tar.gz",  # right prefix, wrong kind
    ]
    listing = _listing(*foreign, *_nightly_keys(range(1, 11)))
    run = _run(tmp_path, *PROD_ARGS, listing=listing)

    assert run.returncode == 0, run.output
    for name in foreign:
        assert not any(name in key for key in run.removed_keys), (
            f"{name} is not a database backup and must never be pruned"
        )
    # The real backups are still subject to policy.
    assert run.removed_keys, "out-of-policy nightly dumps should still be pruned"


def test_retention_still_recognises_the_older_gzip_dumps(tmp_path: Path) -> None:
    """Pre-zstd objects are already in the bucket; they must stay prunable."""
    listing = _listing(
        "freeinference_20260101_040001.sql.gz",
        *_nightly_keys(range(1, 11)),
    )
    run = _run(tmp_path, *PROD_ARGS, listing=listing)

    assert run.returncode == 0, run.output
    assert any("freeinference_20260101_040001.sql.gz" in key for key in run.removed_keys), (
        "a .sql.gz dump far outside the keep set should be pruned, not orphaned forever"
    )


def test_retention_ignores_a_leftover_partial_upload(tmp_path: Path) -> None:
    """A killed run leaves a .partial. It is not a backup and must not hold a slot."""
    listing = _listing(
        "freeinference_20260101_040001.sql.zst.partial",
        *_nightly_keys(range(1, 11)),
    )
    run = _run(tmp_path, *PROD_ARGS, listing=listing)

    assert run.returncode == 0, run.output
    assert "Ignoring leftover partial upload" in run.output
    assert not any(key.endswith(".partial") for key in run.removed_keys)
    # …and the real backup for that date is still judged on its own merits.
    assert any(key.endswith("freeinference_20260101_040001.sql.zst") for key in run.removed_keys)


# ── The GFS keep set, exercised directly ─────────────────────────────────


def _keep_set(dates: list[str], daily: int = 3, weekly: int = 2, monthly: int = 1) -> set[str]:
    """Call compute_keep_set out of the script itself, with no backup running."""
    program = (
        f"source {shlex.quote(str(SCRIPT))}\n"
        f"KEEP_DAILY={daily}\nKEEP_WEEKLY={weekly}\nKEEP_MONTHLY={monthly}\n"
        'printf "%s\\n" "$@" | compute_keep_set\n'
    )
    proc = subprocess.run(
        ["bash", "-c", program, "bash", *dates],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return set(proc.stdout.split())


def _as_date(stamp: str) -> date:
    return date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))


def _iso_week(stamp: str) -> tuple[int, int]:
    return _as_date(stamp).isocalendar()[:2]


def test_sixty_nightly_backups_keep_a_real_grandfather_father_son_spread(
    tmp_path: Path,
) -> None:
    """Sixty consecutive nights, kept with the production 3/2/1 policy.

    The weekly and monthly passes used to start from an empty slate, so they
    re-picked the days immediately behind the daily keeps: the "GFS" policy
    retained six *consecutive* days and nothing older than a week. Losing a
    weekend to a bad dump would have taken every backup with it.
    """
    dates = [(date(2026, 8, 7) - timedelta(days=i)).strftime("%Y%m%d") for i in range(60)]

    kept = _keep_set(dates)

    assert len(kept) == 6, kept
    ordered = sorted(kept, reverse=True)
    dailies, weeklies, monthly = ordered[:3], ordered[3:5], ordered[5]

    assert dailies == dates[:3], "the three most recent nights are the daily keeps"

    daily_weeks = {_iso_week(d) for d in dailies}
    weekly_weeks = [_iso_week(d) for d in weeklies]
    assert len(set(weekly_weeks)) == 2, f"two weekly keeps in one ISO week: {weeklies}"
    assert not daily_weeks & set(weekly_weeks), (
        f"a weekly keep landed in a week the dailies already cover: {ordered}"
    )

    covered_months = {d[:6] for d in dailies + weeklies}
    assert monthly[:6] not in covered_months, (
        f"the monthly keep landed in a month already covered: {ordered}"
    )

    span = (_as_date(ordered[0]) - _as_date(ordered[-1])).days
    assert span >= 30, f"the keep set spans only {span} days: {ordered}"
    assert kept != set(dates[:6]), "this is the six-consecutive-days bug"


def test_fewer_backups_than_slots_keeps_all_of_them(tmp_path: Path) -> None:
    """Nothing is deleted while the policy still has room."""
    dates = ["20260807", "20260806"]

    assert _keep_set(dates) == set(dates)


def test_no_backups_at_all_is_not_an_error(tmp_path: Path) -> None:
    assert _keep_set([]) == set()


# ── Local mode still works ───────────────────────────────────────────────


def test_local_mode_writes_one_compressed_dump_and_leaves_no_partial(tmp_path: Path) -> None:
    """Without --s3-bucket the old behaviour is unchanged: a file on disk."""
    run = _run(tmp_path, "--compress", "--backup-dir", str(tmp_path / "project/backups"))

    assert run.returncode == 0, run.output
    written = _local_dumps(tmp_path)
    assert len(written) == 1, written
    assert written[0].name.endswith(".sql.zst")
    assert written[0].read_text().endswith(SENTINEL + "\n--\n")
    assert not list((tmp_path / "project").rglob("*.partial"))
    assert not run.find("aws s3"), "local mode must not touch S3"


def test_local_mode_leaves_no_dump_behind_when_the_dump_is_truncated(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        "--compress",
        "--backup-dir",
        str(tmp_path / "project/backups"),
        dump=_dump_text(complete=False),
    )

    assert run.returncode != 0
    assert "truncated" in run.output
    assert not _local_dumps(tmp_path), "a truncated dump was left looking like a backup"


@pytest.mark.parametrize("flag", ["--keep-daily", "--keep-weekly", "--keep-monthly"])
def test_a_non_numeric_retention_count_is_refused(tmp_path: Path, flag: str) -> None:
    """Retention arithmetic on a typo'd count is not a thing to discover later."""
    run = _run(tmp_path, *PROD_ARGS, flag, "seven")

    assert run.returncode != 0
    assert "non-negative integer" in run.output
    assert not run.find("docker exec")
