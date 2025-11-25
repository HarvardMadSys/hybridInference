# Database Backup and Restore Scripts

This directory contains utility scripts for backing up and restoring the
HybridInference databases. They are intended to be used before making schema
changes and for periodic backups in production.

## Quick start

### Back up the database

```bash
# Run a default backup (keeps 30 days of backups under ./backups)
./scripts/db/backup.sh

# Backup with gzip compression to save space
./scripts/db/backup.sh --compress

# Use a custom retention period
./scripts/db/backup.sh --retention-days 7
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

### Case 2: Scheduled backups

```bash
# Example crontab entry to run a compressed backup every day at 02:00
0 2 * * * cd /home/murphy/hybridInference && ./scripts/db/backup.sh --compress --retention-days 30
```

## What gets backed up

Currently `backup.sh` backs up the **PostgreSQL** database using `pg_dump`
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
--retention-days N    Keep backups for N days (default: 30)
--backup-dir PATH     Custom backup root directory (default: ./backups)
--compress            Compress PostgreSQL dumps with gzip
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
