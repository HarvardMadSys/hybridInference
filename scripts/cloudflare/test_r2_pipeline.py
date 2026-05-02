#!/usr/bin/env python3
"""Insert fake api_logs into D1 (backdated to yesterday), then run R2 archival.

Usage:
    python scripts/cloudflare/test_r2_pipeline.py              # insert + archive + verify
    python scripts/cloudflare/test_r2_pipeline.py --dry-run    # insert + archive dry-run
    python scripts/cloudflare/test_r2_pipeline.py --cleanup    # delete test rows + R2 object
"""

import asyncio
import gzip
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from serving.storage.d1_client import D1Client

MODELS = ["glm-4.5", "qwen3-32b", "llama-4-scout", "deepseek-r1", "mistral-medium"]
PROVIDERS = ["zhipu", "featherless", "chutes", "ollama"]
OUTCOMES = ["success", "success", "success", "success", "error", "timeout"]  # 67% success

YESTERDAY = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
TEST_PREFIX = "r2test_"


def _make_rows(n: int = 25) -> list[dict]:
    """Generate n fake api_log rows spread across yesterday."""
    import random

    rows = []
    base = datetime.strptime(f"{YESTERDAY}T08:00:00Z", "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    for i in range(n):
        ts = base + timedelta(minutes=random.randint(0, 600))
        outcome = random.choice(OUTCOMES)
        status_code = 200 if outcome == "success" else (504 if outcome == "timeout" else 500)
        rows.append(
            {
                "request_id": f"{TEST_PREFIX}{uuid.uuid4().hex[:16]}",
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "user_id": f"{TEST_PREFIX}user_{i % 5}",
                "model_id": random.choice(MODELS),
                "provider": random.choice(PROVIDERS),
                "cost_usd": round(random.uniform(0.001, 0.05), 6),
                "latency_ms": random.randint(200, 5000),
                "status_code": status_code,
                "ttft_ms": random.randint(50, 800),
                "prompt_tokens": random.randint(50, 2000),
                "completion_tokens": random.randint(10, 1000),
                "outcome": outcome,
            }
        )
    return rows


async def _insert_rows(d1: D1Client, rows: list[dict]) -> int:
    """Batch-insert rows into D1 api_logs. Returns count inserted."""
    cols = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    sql = f"INSERT OR IGNORE INTO api_logs ({col_names}) VALUES ({placeholders})"

    stmts = [(sql, [r[c] for c in cols]) for r in rows]

    # D1 batch limit is 100
    inserted = 0
    for i in range(0, len(stmts), 100):
        batch = stmts[i : i + 100]
        await d1.batch(batch)
        inserted += len(batch)
    return inserted


async def _verify_rows(d1: D1Client) -> int:
    """Count test rows in api_logs."""
    result = await d1.query(
        "SELECT count(*) as cnt FROM api_logs WHERE request_id LIKE ?",
        [f"{TEST_PREFIX}%"],
    )
    return result.rows[0]["cnt"] if result.rows else 0


async def _cleanup(d1: D1Client) -> int:
    """Delete test rows from D1."""
    result = await d1.query(
        "DELETE FROM api_logs WHERE request_id LIKE ?",
        [f"{TEST_PREFIX}%"],
    )
    return result.changes


async def _verify_r2(day: str) -> dict:
    """Check if R2 object exists for the given day."""
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=os.getenv("R2_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )
    bucket = os.getenv("R2_BUCKET_NAME", "hybridinference-logs")
    parts = day.split("-")
    key = f"logs/{parts[0]}/{parts[1]}/{parts[2]}.json.gz"

    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        raw = gzip.decompress(obj["Body"].read()).decode()
        lines = [json.loads(line) for line in raw.strip().split("\n") if line.strip()]
        return {
            "exists": True,
            "key": key,
            "rows": len(lines),
            "sample": lines[0] if lines else None,
        }
    except s3.exceptions.NoSuchKey:
        return {"exists": False, "key": key}
    except Exception as e:
        return {"exists": False, "key": key, "error": str(e)}


async def main():
    """Insert fake api_logs into D1, run R2 archival, and verify results."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--rows", type=int, default=25, help="Number of fake rows")
    args = parser.parse_args()

    d1 = D1Client(
        account_id=os.getenv("D1_ACCOUNT_ID"),
        database_id=os.getenv("D1_DATABASE_ID"),
        api_token=os.getenv("D1_API_TOKEN"),
    )

    try:
        if args.cleanup:
            print(f"Cleaning up test rows (prefix={TEST_PREFIX})...")
            deleted = await _cleanup(d1)
            print(f"  Deleted {deleted} rows from D1")

            # Check R2
            r2 = await _verify_r2(YESTERDAY)
            if r2["exists"]:
                import boto3

                s3 = boto3.client(
                    "s3",
                    endpoint_url=os.getenv("R2_ENDPOINT_URL"),
                    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
                    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
                    region_name="auto",
                )
                bucket = os.getenv("R2_BUCKET_NAME", "hybridinference-logs")
                s3.delete_object(Bucket=bucket, Key=r2["key"])
                print(f"  Deleted R2 object: {r2['key']}")
            print("Cleanup complete.")
            return

        # Step 1: Insert fake rows
        print(f"=== Step 1: Insert {args.rows} fake log rows for {YESTERDAY} ===")
        rows = _make_rows(args.rows)
        inserted = await _insert_rows(d1, rows)
        print(f"  Inserted {inserted} rows into D1 api_logs")

        # Verify
        count = await _verify_rows(d1)
        print(f"  Verified: {count} test rows in D1")

        # Step 2: Run R2 archival
        print(f"\n=== Step 2: Archive {YESTERDAY} to R2 ===")
        dry_flag = "--dry-run" if args.dry_run else ""
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                "scripts/cloudflare/r2_archive_logs.py",
                "--date",
                YESTERDAY,
                *([dry_flag] if dry_flag else []),
            ],
            capture_output=True,
            text=True,
        )
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

        if result.returncode != 0:
            print(f"  Archival exited with code {result.returncode}")
            return

        # Step 3: Verify R2
        if not args.dry_run:
            print("\n=== Step 3: Verify R2 object ===")
            r2 = await _verify_r2(YESTERDAY)
            if r2["exists"]:
                print(f"  R2 object: {r2['key']}")
                print(f"  Rows archived: {r2['rows']}")
                print(f"  Sample row: {json.dumps(r2.get('sample', {}), indent=2)}")
            else:
                print(f"  MISSING: {r2['key']}")
                if "error" in r2:
                    print(f"  Error: {r2['error']}")

            # Step 4: Check if rows were pruned from D1
            print("\n=== Step 4: Check D1 after archival ===")
            remaining = await _verify_rows(d1)
            print(f"  Test rows remaining in D1: {remaining}")
            if remaining == 0:
                print("  All test rows pruned after archival.")
            else:
                print(f"  {remaining} rows still in D1 (may be within retention window)")

        print("\nDone.")
    finally:
        await d1.close()


if __name__ == "__main__":
    asyncio.run(main())
