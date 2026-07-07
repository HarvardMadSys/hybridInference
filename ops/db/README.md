# Database Backup and Restore Scripts

This directory contains utility scripts for backing up and restoring the
HybridInference databases. They are intended to be used before making schema
changes and for periodic backups in production.

## Quick start

### Back up the database

```bash
# Run a default backup (local only, under ./backups)
./ops/db/backup.sh

# Backup with gzip compression to save space
./ops/db/backup.sh --compress

# Backup and upload to S3 (auto-enables compression)
./ops/db/backup.sh --s3-bucket s3://freeinference/backup

# Upload to S3 and remove local copy afterwards
./ops/db/backup.sh --s3-bucket s3://freeinference/backup --s3-only
```

### Restore the database

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
./ops/db/archive-old-logs.sh --s3-archive s3://harvardsys-backup/freeinference/archive/api_logs

# Keep the archive on local disk only (no S3), then delete
./ops/db/archive-old-logs.sh --local-only

# Custom retention window
./ops/db/archive-old-logs.sh --retention-days 90 --s3-archive s3://bucket/prefix
```

Install the monthly cron job (1st of each month, 05:00 UTC, after the nightly backup):

```bash
sudo cp ops/db/archive-old-logs-cron /etc/cron.d/freeinference-archive-logs
sudo chmod 644 /etc/cron.d/freeinference-archive-logs
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

```bash
# Install the cron job (runs daily at 04:00 UTC as the freeinference user)
sudo cp ops/db/backup-cron /etc/cron.d/freeinference-backup
sudo chmod 644 /etc/cron.d/freeinference-backup

# Prerequisites:
#   sudo usermod -aG docker freeinference   # docker access for pg_dump
#   AWS credentials in /home/freeinference/.aws/credentials

# Retention: 3 daily + 2 weekly + 1 monthly (GFS rotation)
# Logs:      /home/freeinference/backup.log
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

By default, backups are stored under:

```text
./backups/backup_YYYYMMDD_HHMMSS/
```

Each backup directory contains:

- PostgreSQL SQL dump file (optionally gzipped).
- `backup_summary.txt` with a human-readable summary and total size.
- Optional `sqlite/` subdirectory if you store SQLite backups there yourself.

## Command-line options

### `backup.sh`

```text
--backup-dir PATH     Custom backup root directory (default: ./backups)
--compress            Compress PostgreSQL dumps with gzip
--s3-bucket URI       Upload backup to S3 (e.g. s3://freeinference/backup)
--s3-only             Upload to S3 and remove local backup after success
--keep-daily N        Keep N most recent daily backups (default: 3)
--keep-weekly N       Keep N most recent weekly backups (default: 2)
--keep-monthly N      Keep N most recent monthly backups (default: 1)
--help                Show help and usage information
```

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

### Low disk space

```bash
# Check disk usage
df -h

# Manually clean up old backups if needed
rm -rf backups/backup_20250101_*
```

## Related documentation

- [Database management guide](../../docs/developer/developer/database.md)
- [Docker Compose configuration](../../deploy/docker/docker-compose.yml)
