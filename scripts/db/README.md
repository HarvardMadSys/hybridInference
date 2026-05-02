# Database Backup and Restore Scripts

This directory contains utility scripts for backing up and restoring the
HybridInference databases. They are intended to be used before making schema
changes and for periodic backups in production.

## Quick start

### Back up the database

```bash
# Run a default backup (local only, under ./backups)
./scripts/db/backup.sh

# Backup with gzip compression to save space
./scripts/db/backup.sh --compress

# Backup and upload to S3 (auto-enables compression)
./scripts/db/backup.sh --s3-bucket s3://freeinference/backup

# Upload to S3 and remove local copy afterwards
./scripts/db/backup.sh --s3-bucket s3://freeinference/backup --s3-only
```

### Restore the database

```bash
# List available backups
ls -lh backups/

# Restore from a specific backup directory
./scripts/db/restore.sh --backup-dir backups/backup_20250114_153000

# Restore only PostgreSQL (skip SQLite)
./scripts/db/restore.sh --backup-dir backups/backup_20250114_153000 --postgres-only

# Restore only SQLite databases (if you have SQLite backups under backup_dir/sqlite)
./scripts/db/restore.sh --backup-dir backups/backup_20250114_153000 --sqlite-only
```

## Typical use cases

### Case 1: Backup before schema changes

```bash
# 1. Take a compressed backup of the current database
./scripts/db/backup.sh --compress

# 2. Apply your schema changes / migrations
#    ... your database changes ...

# 3. If something goes wrong, restore from the backup
./scripts/db/restore.sh --backup-dir backups/backup_20250114_153000
```

### Case 2: Scheduled backups (daily to S3)

```bash
# Install the cron job (runs daily at 04:00 UTC as the freeinference user)
sudo cp scripts/db/backup-cron /etc/cron.d/freeinference-backup
sudo chmod 644 /etc/cron.d/freeinference-backup

# Prerequisites:
#   sudo usermod -aG docker freeinference   # docker access for pg_dump
#   AWS credentials in /home/freeinference/.aws/credentials

# Retention: 3 daily + 2 weekly + 1 monthly (GFS rotation)
# Logs:      /home/freeinference/backup.log
```

## What gets backed up

### When `DB_BACKEND=postgres` (default)

`backup.sh` backs up the **PostgreSQL** database using `pg_dump`
inside the `hybridinference-postgres` container:

1. **PostgreSQL** – primary application database
   (user accounts, API keys, usage logs, etc., depending on your schema).

`restore.sh` supports restoring:

2. **PostgreSQL** – from the SQL dump created by `backup.sh`.
3. **SQLite** – if you place SQLite `.db` or `.db.gz` files under
   `<backup_dir>/sqlite`, `restore.sh` can restore them to `data/db` or
   `var/db` in the project root (for example `rate_limits.db`).

### When `DB_BACKEND=d1` (Cloudflare D1)

PostgreSQL is **not used**. `backup.sh` will exit early with a message
pointing to the D1/R2 scripts. Use these instead:

| Data | Script | Storage |
|------|--------|---------|
| Operational tables (users, keys, sessions, tokens, audit) | `python scripts/cloudflare/d1_backup.py` | Local JSON export |
| API request logs (slim rows, 30-day retention in D1) | `python scripts/cloudflare/r2_archive_logs.py` | Cloudflare R2 (gzipped JSONL) |

```bash
# Export D1 operational tables to timestamped JSON
python scripts/cloudflare/d1_backup.py

# Archive yesterday's logs to R2 and prune rows older than 30 days
python scripts/cloudflare/r2_archive_logs.py

# Dry run (no upload, no delete)
python scripts/cloudflare/r2_archive_logs.py --dry-run

# Only prune old rows without archiving
python scripts/cloudflare/r2_archive_logs.py --prune-only
```

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
docker compose -f infrastructure/docker/docker-compose.yml up -d
```

### Permission issues

```bash
# Ensure scripts are executable
chmod +x scripts/db/*.sh
```

### Low disk space

```bash
# Check disk usage
df -h

# Manually clean up old backups if needed
rm -rf backups/backup_20250101_*
```

## Related documentation

- [Database management guide](../../docs/source/developer/database.md)
- [Docker Compose configuration](../../infrastructure/docker/docker-compose.yml)
