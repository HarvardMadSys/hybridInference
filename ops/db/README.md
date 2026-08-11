# Database Backup and Restore Scripts

This directory contains utility scripts for backing up and restoring the
HybridInference databases. They are intended to be used before making schema
changes and for periodic backups in production.

## Quick start

### Back up the database

```bash
# Run a default backup (local file under ./backups, compressed with zstd)
./ops/db/backup.sh

# Stream the dump straight to S3 — nothing is written to local disk
./ops/db/backup.sh --s3-bucket s3://your-bucket/hybridinference/backup

# --s3-only is still accepted (it is what the installed cron passes) but is
# now a no-op: streaming never produces a local copy to remove.
./ops/db/backup.sh --s3-bucket s3://your-bucket/hybridinference/backup --s3-only
```

> The production database is ~518 GB (`api_logs` alone is 517 GB, 510 GB of it
> TOAST) and compresses to roughly 120 GB, on a host with ~166 GB free on the
> **same filesystem as the Postgres data directory**. A dump written locally
> and then uploaded cannot fit, and filling that volume takes the database down
> with it. That is why `--s3-bucket` streams.

### How a backup is proven good

A truncated pipe still produces a valid `zstd` frame, and `aws s3 cp -` still
completes the multipart upload — so half a dump lands in the bucket looking
perfectly healthy. Size alone proves nothing. Every run therefore:

1. checks the exit status of **every** stage of `pg_dump | tee | zstd | aws`
   (not just the last one — that is what `pipefail` plus `PIPESTATUS` is for);
2. asserts pg_dump's own `-- PostgreSQL database dump complete` sentinel is in
   the last bytes it wrote;
3. uploads to a `<name>.sql.zst.partial` key and only `aws s3 mv`s it onto the
   real key once 1 and 2 pass;
4. re-reads the object's `ContentLength` and refuses anything implausibly small
   (`BACKUP_MIN_OBJECT_BYTES`, default 1 MiB).

Old backups are pruned **only after** that promotion, so a bad night can never
turn into no backups at all.

### Alerting

Nothing watched this job, and four consecutive nights failed unnoticed. The
webhook and `MAILTO` below were added in response — and then five more nights
failed unnoticed in August 2026, because neither was ever actually live: the
installed `/etc/cron.d/freeinference-backup` had drifted from
[backup-cron](backup-cron) and set neither, and the host has no MTA, so `MAILTO`
goes nowhere regardless. Hence the third bullet, which depends only on the
deployed script.

- `BACKUP_ALERT_WEBHOOK_URL` — any failure POSTs a JSON body (`service`,
  `status`, `host`, `stage`, `reason`, `timestamp`, `text`) to that URL. When
  unset it falls back to `SLACK_ALERTS_WEBHOOK_URL` / `SLACK_WEBHOOK_URL` from
  the deployment's `.env` (override the file with `BACKUP_ALERT_ENV_FILE`), so
  failures reach the same Slack channel as every other production alert without
  a second secret to rotate. This covers a run that started and then broke.
- `MAILTO` in [backup-cron](backup-cron), combined with the `|| tail` on the
  cron line, mails the tail of the log on a failing run and stays silent
  otherwise. **Requires an MTA, which this host does not have.**
- [check-backup-health.sh](check-backup-health.sh), run hourly by
  [backup-health-cron](backup-health-cron), watches the job from outside it and
  alerts on the two failures the above cannot see:

  | Condition | Default | Catches |
  |---|---|---|
  | Newest backup older than `--max-age-hours` | 26h | The 04:00 job failed, or never fired at all |
  | Newest backup under `--min-size-pct` of the largest present | 50% | A truncated dump that uploaded cleanly and logged SUCCESS |
  | Free space under `--min-free-gib` | 80 GiB | The volume Postgres and the dump share filling up |

  It reads the uploaded objects rather than `~freeinference/backup.log`, because
  a run that never fired writes nothing to that log and so is indistinguishable
  there from a quiet success. The size baseline is the largest object present,
  not the previous one: against its predecessor a second truncated dump looks
  like healthy growth, which is how a broken backup becomes the new normal.

  Repeat alerts for the same condition are suppressed for 6h so an hourly timer
  cannot post 24 identical messages a day; recovery clears the suppression.

Verify the alert path end to end without waiting for a real failure:

```bash
sudo -u freeinference /srv/hybridInference/ops/db/check-backup-health.sh --test
sudo -u freeinference /srv/hybridInference/ops/db/check-backup-health.sh --dry-run
```

Nothing here detects the monitor itself dying. Point an external
dead-man's-switch (healthchecks.io or equivalent) at the hourly job if you need
that.

### Restore the database

`restore.sh` reads a local backup directory, so a streamed backup has to be
pulled down first — onto a volume with room for it, which on the production
host is **not** the one holding the Postgres data directory:

```bash
aws s3 ls s3://your-bucket/hybridinference/backup/
mkdir -p /mnt/restore/backup_20260807_040001
aws s3 cp s3://your-bucket/hybridinference/backup/freeinference_20260807_040001.sql.zst \
    /mnt/restore/backup_20260807_040001/
./ops/db/restore.sh --backup-dir /mnt/restore/backup_20260807_040001
```

```bash
# List available backups
ls -lh backups/

# Restore from a specific backup directory
./ops/db/restore.sh --backup-dir backups/backup_20250114_153000

# Restore only PostgreSQL (skip SQLite)
./ops/db/restore.sh --backup-dir backups/backup_20250114_153000 --postgres-only

# Restore only SQLite databases (if you have SQLite backups under backup_dir/sqlite)
./ops/db/restore.sh --backup-dir backups/backup_20250114_153000 --sqlite-only
```

### Archive & prune old `api_logs` rows

`archive-old-logs.sh` exports `api_logs` rows older than a retention window to a
compressed CSV, uploads it to S3, verifies the upload, and only then deletes
those rows from the database.

```bash
# Preview: how many rows are older than 180 days (no changes made)
./ops/db/archive-old-logs.sh --dry-run

# Archive to S3, verify, then delete (recommended)
./ops/db/archive-old-logs.sh --s3-archive s3://your-bucket/hybridinference/archive/api_logs

# Keep the archive on local disk only (no S3), then delete
./ops/db/archive-old-logs.sh --local-only

# Custom retention window
./ops/db/archive-old-logs.sh --retention-days 90 --s3-archive s3://bucket/prefix
```

Schedule it monthly, after the nightly backup. Write the entry for the user
that owns your AWS credentials and your checkout:

```cron
# /etc/cron.d/hybridinference-archive-logs
SHELL=/bin/bash
HOME=/home/<user>
0 5 1 * * <user> /path/to/hybridInference/ops/db/archive-old-logs.sh \
    --retention-days 180 --s3-archive s3://your-bucket/hybridinference/archive/api_logs >> ~/archive-logs.log 2>&1
```

> ⚠️ `api_logs` contains user prompts/responses (**PII**). The `--s3-archive`
> target must be a location you are authorized to store that data in. Deletion
> only runs after the archive is written, integrity-checked, row-count-verified,
> and (if uploading) size-verified on S3; the delete itself is guarded by a
> transaction that rolls back if the deleted count doesn't match the archive.
> Note that `DELETE` frees space for reuse inside the table but does **not**
> return disk to the OS (the script does not run `VACUUM FULL`).

## Typical use cases

### Case 1: Backup before schema changes

```bash
# 1. Take a compressed backup of the current database
./ops/db/backup.sh --compress

# 2. Apply your schema changes / migrations
#    ... your database changes ...

# 3. If something goes wrong, restore from the backup
./ops/db/restore.sh --backup-dir backups/backup_20250114_153000
```

### Case 2: Scheduled backups (daily to S3)

[`backup-cron`](backup-cron) is the production entry, ready to install:

```bash
sudo cp ops/db/backup-cron /etc/cron.d/freeinference-backup
```

Generalised, it is:

```cron
# /etc/cron.d/hybridinference-backup — daily at 04:00 UTC
SHELL=/bin/bash
HOME=/home/<user>
MAILTO=<you@example.com>
0 4 * * * <user> /path/to/hybridInference/ops/db/backup.sh \
    --compress --s3-bucket s3://your-bucket/hybridinference/backup >> ~/backup.log 2>&1 \
    || tail -n 40 ~/backup.log
```

`>> log 2>&1` on its own would swallow the failure too, leaving `MAILTO` with
nothing to send; the trailing `|| tail` is what turns a failing run into mail.

```bash
# Prerequisites for <user>:
#   sudo usermod -aG docker <user>          # docker access for pg_dump
#   AWS credentials in ~<user>/.aws/credentials
#   (verified up front — the run fails in seconds, not after a multi-hour dump)

# Retention: 3 daily + 2 weekly + 1 monthly (GFS rotation), applied only after
#            the new backup is verified
# Logs:      wherever the cron entry above redirects them
```

## What gets backed up

`backup.sh` backs up the **PostgreSQL** database using `pg_dump`
inside the `hybridinference-postgres` container:

1. **PostgreSQL** – primary application database
   (user accounts, API keys, usage logs, etc., depending on your schema).

`restore.sh` supports restoring:

2. **PostgreSQL** – from the SQL dump created by `backup.sh`.
3. **SQLite** – if you place SQLite `.db` or `.db.gz` files under
   `<backup_dir>/sqlite`, `restore.sh` can restore them to `data/db` or
   `var/db` in the project root (for example `rate_limits.db`).

## Backup location

**Streaming (`--s3-bucket`)** writes nothing locally. The object lands at:

```text
s3://<bucket>/<prefix>/<DB_NAME>_YYYYMMDD_HHMMSS.sql.zst
```

**Local mode** (no `--s3-bucket`) stores backups under:

```text
./backups/backup_YYYYMMDD_HHMMSS/
```

Each backup directory contains:

- The PostgreSQL dump (`<DB_NAME>_YYYYMMDD_HHMMSS.sql.zst`, or `.sql` with
  `--no-compress`).
- `backup_summary.txt` with a human-readable summary and total size.
- Optional `sqlite/` subdirectory if you store SQLite backups there yourself.

A backup still in flight is named `<name>.partial` and is renamed only once it
verifies, so anything without that suffix has passed every check.

## Retention (GFS)

Retention keeps the `--keep-daily` most recent backups, then `--keep-weekly`
more — one per ISO week, **skipping the weeks the daily keeps already cover** —
then `--keep-monthly` more, one per calendar month, skipping months already
covered. Sixty consecutive nightly dumps with the defaults therefore keep six
backups spread over roughly two months, not six consecutive days.

## Command-line options

### `backup.sh`

```text
--backup-dir PATH     Local backup root (default: ./backups); ignored when
                      streaming to S3
--compress            Compress with zstd (default: on)
--no-compress         Write an uncompressed .sql dump (local mode only)
--s3-bucket URI       Stream the dump to S3 (e.g. s3://your-bucket/hybridinference/backup)
--s3-only             Accepted for backwards compatibility; a no-op, since
                      streaming never writes a local dump
--keep-daily N        Keep the N most recent backups (default: 3)
--keep-weekly N       Keep N more, one per ISO week (default: 2)
--keep-monthly N      Keep N more, one per calendar month (default: 1)
--help                Show help and usage information
```

Environment variables:

```text
DB_NAME, DB_USER, DB_PASSWORD   PostgreSQL credentials (read from .env)
BACKUP_ALERT_WEBHOOK_URL        POST a JSON failure report here (optional)
BACKUP_EXPECTED_SIZE            --expected-size for the S3 upload (default 250 GB)
BACKUP_MIN_OBJECT_BYTES         Reject an uploaded object smaller than this
BACKUP_PROJECT_ROOT             Override the detected project root
POSTGRES_CONTAINER              Container to run pg_dump in
DOCKER_COMPOSE_FILE             Compose file named in the "not running" hint
```

`BACKUP_EXPECTED_SIZE` is not an optional tuning knob: `aws s3 cp -` from a
stream defaults to 8 MB × 10 000 parts, an 80 GB ceiling that a ~120 GB dump
hits hours into the upload. Over-estimating only enlarges the part size, so the
default is deliberately generous.

### `restore.sh`

```text
--backup-dir PATH     Backup directory to restore from (required)
--postgres-only       Only restore PostgreSQL
--sqlite-only         Only restore SQLite databases
--force               Skip confirmation prompts (dangerous!)
--help                Show help and usage information
```

### `archive-old-logs.sh`

```text
--retention-days N    Archive+delete rows older than N days (default: 180)
--s3-archive URI      S3 prefix for archives; uploaded and size-verified
                      before any delete
--local-only          Keep the archive on local disk only (no S3); required
                      to permit deletion when --s3-archive is unset
--backup-dir PATH     Local dir for the archive file (default: ./backups/archive)
--keep-local          Keep the local archive after a successful S3 upload
--table NAME          Table to prune (default: api_logs)
--ts-column NAME      Timestamp column to compare (default: timestamp)
--dry-run             Report counts/size only; write nothing, delete nothing
--help                Show help and usage information
```

## Notes and warnings

⚠️ **Important:**

- Restore operations will **overwrite existing databases**.
- Unless you pass `--force`, `restore.sh` asks for an explicit confirmation.
- The PostgreSQL container (`hybridinference-postgres`) must be running.
- Both scripts load credentials from the project `.env` file:
  - `DB_NAME`, `DB_USER`, `DB_PASSWORD`.
- `backup.sh` runs its preflight (container up, credentials present,
  `aws sts get-caller-identity`, bucket reachable) **before** the dump, so a
  misconfigured run fails in seconds rather than after several hours. The first
  real-world failure of this script burned a full dump and only then discovered
  that the credentials were unresolvable.
- A restore has never been rehearsed against a dump this size. Treat "the
  object is in the bucket and verified" as necessary, not sufficient.

## Troubleshooting

### PostgreSQL container is not running

```bash
# Start PostgreSQL via Docker Compose
docker compose -f deploy/docker/docker-compose.yml up -d
```

### Permission issues

```bash
# Ensure scripts are executable
chmod +x ops/db/*.sh
```

### `aws sts get-caller-identity` fails in preflight

The AWS CLI reads the *invoking* user's `~/.aws`. Under cron that is the user
named in the crontab line, not you — run
`sudo -u freeinference aws sts get-caller-identity` to reproduce it.

### The bucket is not reachable

`backup.sh` refuses to start when `aws s3api head-bucket` fails. Check the
bucket really exists (`aws s3 ls`) — a typo'd or renamed bucket is exactly the
failure that used to be discovered only after a full dump had been produced.

### Low disk space

```bash
# Check disk usage
df -h

# Manually clean up old backups if needed
rm -rf backups/backup_20250101_*
```

Streaming to S3 does not consume local disk, so this applies to local-mode
backups only. Do not switch a large database to local mode on the volume that
holds the Postgres data directory — filling it takes the database down.

### Leftover `.partial` objects

An interrupted upload leaves `<name>.sql.zst.partial` behind (the script tries
to delete it, but a killed process cannot). Retention ignores `.partial`
objects rather than counting them as backups; delete them by hand when they
accumulate.

## Related documentation

- [Database management guide](../../docs/developer/database.md)
- [Docker Compose configuration](../../deploy/docker/docker-compose.yml)
