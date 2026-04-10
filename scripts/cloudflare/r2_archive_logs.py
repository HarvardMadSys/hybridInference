#!/usr/bin/env python3
"""Archive D1 api_logs to Cloudflare R2 and prune old rows.

Designed to run daily (e.g. via cron or Cloudflare Workers Cron Trigger).
Each invocation archives one day's logs into a single gzipped JSON file
in R2, then deletes the archived rows from D1.

Usage:
    # Archive yesterday's logs (default)
    python scripts/cloudflare/r2_archive_logs.py

    # Archive a specific date
    python scripts/cloudflare/r2_archive_logs.py --date 2026-04-01

    # Dry run (no upload, no delete)
    python scripts/cloudflare/r2_archive_logs.py --dry-run

    # Only prune rows older than retention period (no archive)
    python scripts/cloudflare/r2_archive_logs.py --prune-only

R2 layout:
    s3://{bucket}/logs/{YYYY}/{MM}/{DD}.json.gz

Each .json.gz file contains one JSON object per line (JSONL format).
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv

from serving.config.settings import get_settings
from serving.storage.d1_client import D1Client
from serving.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)

# D1 query page size (stay under D1 limits)
PAGE_SIZE = 500
# D1 delete batch size (max 100 statements per batch)
DELETE_BATCH_SIZE = 80


async def _fetch_day_logs(d1: D1Client, day: str) -> list[dict]:
    """Fetch all api_logs rows for a given day from D1.

    Args:
        d1: D1 client instance.
        day: Date string YYYY-MM-DD.

    Returns:
        List of row dicts.
    """
    day_start = f"{day}T00:00:00.000000Z"
    day_end = f"{day}T23:59:59.999999Z"

    all_rows: list[dict] = []
    offset = 0

    while True:
        result = await d1.query(
            "SELECT * FROM api_logs "
            "WHERE timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp ASC "
            "LIMIT ? OFFSET ?",
            [day_start, day_end, PAGE_SIZE, offset],
        )
        if not result.rows:
            break
        all_rows.extend(result.rows)
        if len(result.rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return all_rows


def _compress_rows(rows: list[dict]) -> bytes:
    """Compress rows as gzipped JSONL."""
    lines = [json.dumps(row, default=str, separators=(",", ":")) for row in rows]
    payload = "\n".join(lines).encode("utf-8")
    return gzip.compress(payload, compresslevel=6)


async def _upload_to_r2(
    data: bytes,
    key: str,
    *,
    bucket: str,
    endpoint_url: str,
    access_key_id: str,
    secret_access_key: str,
) -> None:
    """Upload gzipped data to R2 via S3-compatible API."""
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError:
        logger.error("boto3 is required for R2 uploads. Install with: pip install boto3")
        raise

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=BotoConfig(
            region_name="auto",
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        ContentType="application/gzip",
        ContentEncoding="gzip",
    )
    logger.info("Uploaded %s to R2 bucket %s (%d bytes)", key, bucket, len(data))


async def _delete_day_logs(d1: D1Client, day: str) -> int:
    """Delete all api_logs rows for a given day from D1.

    Returns:
        Number of rows deleted.
    """
    day_start = f"{day}T00:00:00.000000Z"
    day_end = f"{day}T23:59:59.999999Z"

    total_deleted = 0

    while True:
        # Fetch a batch of request_ids to delete
        result = await d1.query(
            "SELECT request_id FROM api_logs WHERE timestamp >= ? AND timestamp <= ? LIMIT ?",
            [day_start, day_end, DELETE_BATCH_SIZE],
        )
        if not result.rows:
            break

        # Batch delete
        stmts = [
            ("DELETE FROM api_logs WHERE request_id = ?", [row["request_id"]])
            for row in result.rows
        ]
        await d1.batch(stmts)
        total_deleted += len(result.rows)

    return total_deleted


async def _prune_old_logs(d1: D1Client, retention_days: int) -> int:
    """Delete api_logs rows older than the retention period.

    Returns:
        Number of rows deleted.
    """
    cutoff_day = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    cutoff_ts = f"{cutoff_day}T00:00:00.000000Z"

    total_deleted = 0

    while True:
        result = await d1.query(
            "SELECT request_id FROM api_logs WHERE timestamp < ? LIMIT ?",
            [cutoff_ts, DELETE_BATCH_SIZE],
        )
        if not result.rows:
            break

        stmts = [
            ("DELETE FROM api_logs WHERE request_id = ?", [row["request_id"]])
            for row in result.rows
        ]
        await d1.batch(stmts)
        total_deleted += len(result.rows)

    return total_deleted


async def archive_day(
    d1: D1Client,
    day: str,
    *,
    dry_run: bool = False,
    bucket: str,
    endpoint_url: str,
    access_key_id: str,
    secret_access_key: str,
) -> dict:
    """Archive a single day's logs to R2.

    Returns:
        Summary dict with row_count, compressed_size, r2_key, deleted.
    """
    logger.info("Archiving logs for %s ...", day)

    rows = await _fetch_day_logs(d1, day)
    if not rows:
        logger.info("No logs found for %s — nothing to archive", day)
        return {"day": day, "row_count": 0, "skipped": True}

    compressed = _compress_rows(rows)

    # R2 key: logs/YYYY/MM/DD.json.gz
    parts = day.split("-")
    r2_key = f"logs/{parts[0]}/{parts[1]}/{parts[2]}.json.gz"

    summary = {
        "day": day,
        "row_count": len(rows),
        "compressed_bytes": len(compressed),
        "r2_key": r2_key,
    }

    if dry_run:
        logger.info(
            "[DRY RUN] Would upload %d rows (%d bytes) to %s", len(rows), len(compressed), r2_key
        )
        summary["dry_run"] = True
        return summary

    # Upload
    await _upload_to_r2(
        compressed,
        r2_key,
        bucket=bucket,
        endpoint_url=endpoint_url,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
    )

    # Delete from D1 after successful upload
    deleted = await _delete_day_logs(d1, day)
    summary["deleted"] = deleted
    logger.info(
        "Archived %s: %d rows, %d bytes compressed, %d deleted from D1",
        day,
        len(rows),
        len(compressed),
        deleted,
    )

    return summary


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Archive D1 api_logs to R2")
    parser.add_argument(
        "--date",
        help="Date to archive (YYYY-MM-DD). Default: yesterday.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and compress but don't upload or delete.",
    )
    parser.add_argument(
        "--prune-only",
        action="store_true",
        help="Only prune rows older than retention period (no archive).",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        help="Override retention period (default: from settings).",
    )
    return parser.parse_args()


async def main() -> None:
    """Run the archival process."""
    load_dotenv()
    setup_logging()

    args = _parse_args()
    settings = get_settings()

    if not all([settings.d1_account_id, settings.d1_database_id, settings.d1_api_token]):
        logger.error(
            "D1 credentials not configured. Set D1_ACCOUNT_ID, D1_DATABASE_ID, D1_API_TOKEN."
        )
        sys.exit(1)

    d1 = D1Client(
        account_id=settings.d1_account_id,
        database_id=settings.d1_database_id,
        api_token=settings.d1_api_token,
    )

    try:
        retention_days = args.retention_days or settings.r2_log_retention_days

        if args.prune_only:
            deleted = await _prune_old_logs(d1, retention_days)
            logger.info("Pruned %d rows older than %d days", deleted, retention_days)
            return

        # Validate R2 config
        if not args.dry_run and not all(
            [
                settings.r2_access_key_id,
                settings.r2_secret_access_key,
                settings.r2_endpoint_url,
            ]
        ):
            logger.error(
                "R2 credentials not configured. "
                "Set R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT_URL."
            )
            sys.exit(1)

        # Determine target date
        if args.date:
            target_day = args.date
        else:
            target_day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

        summary = await archive_day(
            d1,
            target_day,
            dry_run=args.dry_run,
            bucket=settings.r2_bucket_name,
            endpoint_url=settings.r2_endpoint_url,
            access_key_id=settings.r2_access_key_id,
            secret_access_key=settings.r2_secret_access_key,
        )

        # Also prune anything older than retention period
        if not args.dry_run:
            pruned = await _prune_old_logs(d1, retention_days)
            if pruned:
                logger.info("Pruned %d additional rows older than %d days", pruned, retention_days)
            summary["pruned"] = pruned

        print(json.dumps(summary, indent=2))

    finally:
        await d1.close()


if __name__ == "__main__":
    asyncio.run(main())
