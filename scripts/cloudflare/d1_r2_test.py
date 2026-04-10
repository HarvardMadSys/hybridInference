#!/usr/bin/env python3
"""R2 archival integration test suite.

Tests the full lifecycle: D1 insert → archive to R2 → verify R2 → prune D1.

Usage:
    python scripts/cloudflare/d1_r2_test.py               # run all tests
    python scripts/cloudflare/d1_r2_test.py --cleanup      # remove test data only
"""

import asyncio
import gzip
import json
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from serving.storage.d1_client import D1Client

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TEST_PREFIX = "r2t_"
MODELS = ["glm-4.5", "qwen3-32b", "llama-4-scout", "deepseek-r1", "mistral-medium"]
PROVIDERS = ["zhipu", "featherless", "chutes", "ollama"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_rows(day: str, n: int) -> list[dict]:
    """Generate n fake api_log rows for a given day."""
    base = datetime.strptime(f"{day}T06:00:00Z", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        ts = base + timedelta(minutes=random.randint(0, 720))
        outcome = random.choice(["success", "success", "success", "error", "timeout"])
        status_code = {"success": 200, "error": 500, "timeout": 504}[outcome]
        rows.append(
            {
                "request_id": f"{TEST_PREFIX}{uuid.uuid4().hex[:16]}",
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "user_id": f"{TEST_PREFIX}user_{i % 5}",
                "model_id": random.choice(MODELS),
                "provider": random.choice(PROVIDERS),
                "cost_usd": round(random.uniform(0.001, 0.05), 6),
                "latency_ms": random.randint(100, 5000),
                "status_code": status_code,
                "ttft_ms": random.randint(30, 800),
                "prompt_tokens": random.randint(50, 2000),
                "completion_tokens": random.randint(10, 1000),
                "outcome": outcome,
            }
        )
    return rows


async def _insert_rows(d1: D1Client, rows: list[dict]) -> int:
    cols = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    sql = f"INSERT OR IGNORE INTO api_logs ({col_names}) VALUES ({placeholders})"
    stmts = [(sql, [r[c] for c in cols]) for r in rows]
    for i in range(0, len(stmts), 100):
        await d1.batch(stmts[i : i + 100])
    return len(rows)


async def _count_test_rows(d1: D1Client, day: str | None = None) -> int:
    if day:
        result = await d1.query(
            "SELECT count(*) as cnt FROM api_logs WHERE request_id LIKE ? "
            "AND timestamp >= ? AND timestamp <= ?",
            [f"{TEST_PREFIX}%", f"{day}T00:00:00.000000Z", f"{day}T23:59:59.999999Z"],
        )
    else:
        result = await d1.query(
            "SELECT count(*) as cnt FROM api_logs WHERE request_id LIKE ?",
            [f"{TEST_PREFIX}%"],
        )
    return result.rows[0]["cnt"] if result.rows else 0


async def _cleanup_d1(d1: D1Client) -> int:
    result = await d1.execute("DELETE FROM api_logs WHERE request_id LIKE ?", [f"{TEST_PREFIX}%"])
    return result.changes


def _get_s3_client():
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        endpoint_url=os.getenv("R2_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        config=BotoConfig(
            region_name="auto",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def _r2_key(day: str) -> str:
    parts = day.split("-")
    return f"logs/{parts[0]}/{parts[1]}/{parts[2]}.json.gz"


def _r2_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def _r2_read(s3, bucket: str, key: str) -> list[dict]:
    resp = s3.get_object(Bucket=bucket, Key=key)
    data = resp["Body"].read()
    try:
        text = gzip.decompress(data).decode()
    except gzip.BadGzipFile:
        text = data.decode()
    return [json.loads(line) for line in text.strip().split("\n") if line.strip()]


def _r2_delete(s3, bucket: str, key: str) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        s3.delete_object(Bucket=bucket, Key=key)


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------
class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def ok(self, name: str):
        self.passed += 1
        print(f"  [OK]   {name}")

    def fail(self, name: str, detail: str):
        self.failed += 1
        self.errors.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} — {detail}")


async def _run_tests():
    d1 = D1Client(
        account_id=os.getenv("D1_ACCOUNT_ID"),
        database_id=os.getenv("D1_DATABASE_ID"),
        api_token=os.getenv("D1_API_TOKEN"),
    )
    bucket = os.getenv("R2_BUCKET_NAME", "hybridinference-logs")
    s3 = _get_s3_client()
    r = TestResult()

    # Use dates far in the past to avoid collisions
    day1 = "2020-01-15"
    day2 = "2020-01-16"
    day3 = "2020-01-17"

    try:
        # Clean slate
        await _cleanup_d1(d1)
        for d in [day1, day2, day3]:
            _r2_delete(s3, bucket, _r2_key(d))

        # =================================================================
        print("\n=== Test 1: Archive single day ===")
        # =================================================================
        rows1 = _make_rows(day1, 20)
        await _insert_rows(d1, rows1)

        cnt = await _count_test_rows(d1, day1)
        if cnt == 20:
            r.ok("insert_20_rows")
        else:
            r.fail("insert_20_rows", f"expected 20, got {cnt}")

        # Run archival
        from scripts.cloudflare.r2_archive_logs import archive_day

        summary = await archive_day(
            d1,
            day1,
            dry_run=False,
            bucket=bucket,
            endpoint_url=os.getenv("R2_ENDPOINT_URL"),
            access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
            secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        )

        if summary.get("row_count") == 20:
            r.ok("archive_row_count")
        else:
            r.fail("archive_row_count", f"expected 20, got {summary.get('row_count')}")

        if summary.get("deleted", 0) >= 20:
            r.ok("d1_rows_deleted")
        else:
            r.fail("d1_rows_deleted", f"expected >=20 deleted, got {summary.get('deleted')}")

        # Verify D1 is empty for that day
        remaining = await _count_test_rows(d1, day1)
        if remaining == 0:
            r.ok("d1_empty_after_archive")
        else:
            r.fail("d1_empty_after_archive", f"{remaining} rows still in D1")

        # Verify R2 object
        if _r2_exists(s3, bucket, _r2_key(day1)):
            r.ok("r2_object_exists")
        else:
            r.fail("r2_object_exists", "object not found in R2")

        # Verify R2 content
        r2_rows = _r2_read(s3, bucket, _r2_key(day1))
        if len(r2_rows) == 20:
            r.ok("r2_row_count")
        else:
            r.fail("r2_row_count", f"expected 20, got {len(r2_rows)}")

        # =================================================================
        print("\n=== Test 2: Data integrity (round-trip) ===")
        # =================================================================
        # Check all fields survived the round-trip
        original_ids = {row["request_id"] for row in rows1}
        archived_ids = {row["request_id"] for row in r2_rows}
        if original_ids == archived_ids:
            r.ok("request_ids_match")
        else:
            missing = original_ids - archived_ids
            r.fail("request_ids_match", f"{len(missing)} IDs missing from archive")

        # Spot-check fields on first row
        sample = next((ar for ar in r2_rows if ar["request_id"] == rows1[0]["request_id"]), None)
        if sample:
            fields_ok = True
            for field in ["model_id", "provider", "user_id", "outcome", "status_code"]:
                if str(sample.get(field)) != str(rows1[0].get(field)):
                    r.fail(f"field_{field}", f"expected {rows1[0][field]}, got {sample.get(field)}")
                    fields_ok = False
            if fields_ok:
                r.ok("field_values_preserved")
        else:
            r.fail("field_values_preserved", "sample row not found in archive")

        # =================================================================
        print("\n=== Test 3: Empty day (no logs) ===")
        # =================================================================
        summary_empty = await archive_day(
            d1,
            day2,
            dry_run=False,
            bucket=bucket,
            endpoint_url=os.getenv("R2_ENDPOINT_URL"),
            access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
            secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        )
        if summary_empty.get("skipped") is True and summary_empty.get("row_count") == 0:
            r.ok("empty_day_skipped")
        else:
            r.fail("empty_day_skipped", f"expected skip, got {summary_empty}")

        if not _r2_exists(s3, bucket, _r2_key(day2)):
            r.ok("no_r2_object_for_empty_day")
        else:
            r.fail("no_r2_object_for_empty_day", "object should not exist")

        # =================================================================
        print("\n=== Test 4: Idempotent re-archive ===")
        # =================================================================
        # Insert rows for day3, archive, then re-insert and archive again
        rows3a = _make_rows(day3, 10)
        await _insert_rows(d1, rows3a)
        await archive_day(
            d1,
            day3,
            dry_run=False,
            bucket=bucket,
            endpoint_url=os.getenv("R2_ENDPOINT_URL"),
            access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
            secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        )
        first_archive = _r2_read(s3, bucket, _r2_key(day3))

        # Insert more rows and re-archive (overwrites)
        rows3b = _make_rows(day3, 5)
        await _insert_rows(d1, rows3b)
        await archive_day(
            d1,
            day3,
            dry_run=False,
            bucket=bucket,
            endpoint_url=os.getenv("R2_ENDPOINT_URL"),
            access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
            secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        )
        second_archive = _r2_read(s3, bucket, _r2_key(day3))

        if len(first_archive) == 10:
            r.ok("first_archive_10_rows")
        else:
            r.fail("first_archive_10_rows", f"got {len(first_archive)}")

        if len(second_archive) == 5:
            r.ok("re_archive_overwrites")
        else:
            r.fail("re_archive_overwrites", f"expected 5, got {len(second_archive)}")

        remaining3 = await _count_test_rows(d1, day3)
        if remaining3 == 0:
            r.ok("d1_clean_after_re_archive")
        else:
            r.fail("d1_clean_after_re_archive", f"{remaining3} rows remain")

        # =================================================================
        print("\n=== Test 5: Dry run does not upload or delete ===")
        # =================================================================
        rows_dry = _make_rows(day2, 8)
        await _insert_rows(d1, rows_dry)

        summary_dry = await archive_day(
            d1,
            day2,
            dry_run=True,
            bucket=bucket,
            endpoint_url=os.getenv("R2_ENDPOINT_URL"),
            access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
            secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        )

        if summary_dry.get("dry_run") is True:
            r.ok("dry_run_flag")
        else:
            r.fail("dry_run_flag", "expected dry_run=True")

        if not _r2_exists(s3, bucket, _r2_key(day2)):
            r.ok("dry_run_no_upload")
        else:
            r.fail("dry_run_no_upload", "R2 object should not exist after dry run")

        d1_after_dry = await _count_test_rows(d1, day2)
        if d1_after_dry == 8:
            r.ok("dry_run_no_delete")
        else:
            r.fail("dry_run_no_delete", f"expected 8 rows, got {d1_after_dry}")

        # =================================================================
        print("\n=== Test 6: Multi-day batch ===")
        # =================================================================
        # Clean day2 leftovers from dry run, then insert across day2 and day3
        await _cleanup_d1(d1)
        for d in [day2, day3]:
            _r2_delete(s3, bucket, _r2_key(d))

        await _insert_rows(d1, _make_rows(day2, 15))
        await _insert_rows(d1, _make_rows(day3, 12))

        for day, expected in [(day2, 15), (day3, 12)]:
            await archive_day(
                d1,
                day,
                dry_run=False,
                bucket=bucket,
                endpoint_url=os.getenv("R2_ENDPOINT_URL"),
                access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
                secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
            )
            archived = _r2_read(s3, bucket, _r2_key(day))
            if len(archived) == expected:
                r.ok(f"multi_day_{day}_{expected}_rows")
            else:
                r.fail(f"multi_day_{day}_{expected}_rows", f"got {len(archived)}")

        total_remaining = await _count_test_rows(d1)
        if total_remaining == 0:
            r.ok("multi_day_d1_clean")
        else:
            r.fail("multi_day_d1_clean", f"{total_remaining} rows remain")

        # =================================================================
        print("\n=== Test 7: Large batch (200 rows) ===")
        # =================================================================
        _r2_delete(s3, bucket, _r2_key(day1))
        await _insert_rows(d1, _make_rows(day1, 200))

        s_large = await archive_day(
            d1,
            day1,
            dry_run=False,
            bucket=bucket,
            endpoint_url=os.getenv("R2_ENDPOINT_URL"),
            access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
            secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        )
        if s_large.get("row_count") == 200:
            r.ok("large_batch_archived")
        else:
            r.fail("large_batch_archived", f"expected 200, got {s_large.get('row_count')}")

        large_r2 = _r2_read(s3, bucket, _r2_key(day1))
        if len(large_r2) == 200:
            r.ok("large_batch_r2_complete")
        else:
            r.fail("large_batch_r2_complete", f"expected 200, got {len(large_r2)}")

        if s_large.get("compressed_bytes", 0) > 0:
            ratio = s_large["row_count"] * 150 / s_large["compressed_bytes"]
            r.ok(f"compression_ratio_{ratio:.1f}x")
        else:
            r.fail("compression_ratio", "no compressed_bytes")

        remaining_large = await _count_test_rows(d1, day1)
        if remaining_large == 0:
            r.ok("large_batch_d1_clean")
        else:
            r.fail("large_batch_d1_clean", f"{remaining_large} rows remain")

    finally:
        # Cleanup
        print("\n=== Cleanup ===")
        deleted = await _cleanup_d1(d1)
        print(f"  D1: deleted {deleted} test rows")
        for d in [day1, day2, day3]:
            _r2_delete(s3, bucket, _r2_key(d))
            print(f"  R2: deleted {_r2_key(d)}")
        await d1.close()

    # Summary
    print(f"\n{'=' * 60}")
    print(f"Results: {r.passed}/{r.passed + r.failed} passed, {r.failed} failed")
    if r.errors:
        print("\nFailures:")
        for e in r.errors:
            print(f"  - {e}")
    return r.failed


async def _cleanup_only():
    d1 = D1Client(
        account_id=os.getenv("D1_ACCOUNT_ID"),
        database_id=os.getenv("D1_DATABASE_ID"),
        api_token=os.getenv("D1_API_TOKEN"),
    )
    bucket = os.getenv("R2_BUCKET_NAME", "hybridinference-logs")
    s3 = _get_s3_client()

    deleted = await _cleanup_d1(d1)
    print(f"D1: deleted {deleted} test rows")

    for d in ["2020-01-15", "2020-01-16", "2020-01-17"]:
        _r2_delete(s3, bucket, _r2_key(d))
        print(f"R2: deleted {_r2_key(d)}")

    await d1.close()
    print("Cleanup complete.")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="R2 archival integration tests")
    parser.add_argument("--cleanup", action="store_true", help="Remove test data only")
    args = parser.parse_args()

    if args.cleanup:
        asyncio.run(_cleanup_only())
    else:
        failures = asyncio.run(_run_tests())
        sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
