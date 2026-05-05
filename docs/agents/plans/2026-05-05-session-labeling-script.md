# Session Labeling Script Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write a standalone Python script that reads all rows from `api_logs`, assigns a `session_id` to each request using temporal-clustering heuristics (gap-based session boundary detection), writes results back to the database, and produces a summary report.

**Architecture:** A single script `ops/db/label_sessions.py` that connects to PostgreSQL, fetches all `api_logs` rows ordered by `(user_agent, timestamp)`, walks them sequentially assigning session IDs using two configurable thresholds (inactivity gap and user-agent change), then batch-updates the `session_id` column. A dry-run mode is the default — it prints a summary without writing. The `--write` flag is required to actually persist results.

**Tech Stack:** Python 3.12, `asyncpg` (already in project dependencies), `dataclasses`. No new dependencies.

**Database connection:** Same env vars used by the app: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.

---

## Algorithm Design

### Session boundary heuristic

Group requests by `(user_id, user_agent)`. Within each group, sort by `timestamp`. Walk sequentially; start a new session when:

1. **Inactivity gap exceeded:** The gap between the current request and the previous request exceeds `SESSION_GAP_MINUTES` (default: **10 minutes**). Data analysis shows that 96% of within-session gaps are under 5 minutes. A 10-minute threshold captures bursts with brief thinking pauses while splitting clearly separate interactions.

2. **(Future extension point)** Model change is NOT a session boundary — agents routinely switch models mid-conversation (e.g., claude-cli switching between opus and sonnet within one session).

The session ID format is `sess_{group_hash}_{sequence_number}`, e.g. `sess_a1b2c3_001`. The group hash is derived from `(user_id, user_agent)` to make sessions self-describing.

### Why not `(user_id, user_agent, model_id)` grouping?

Data shows claude-cli interleaves requests to `claude-opus-4.7` and `claude-sonnet-4.6` within the same continuous interaction (model switches with sub-second gaps). Grouping by model would incorrectly split one session into two.

### Gap threshold analysis

From the actual database:

| Gap range | Count | % |
|---|---|---|
| < 30s | 5784 | 90.6% |
| 30s–1m | 210 | 3.3% |
| 1–2m | 131 | 2.1% |
| 2–5m | 150 | 2.3% |
| 5–10m | 52 | 0.8% |
| 10–30m | 45 | 0.7% |
| 30m+ | 40 | 0.6% |

A 10-minute threshold: keeps 96.8% of same-group consecutive pairs in the same session while splitting at natural breaks.

---

## File Structure

```
ops/db/label_sessions.py          # Main script (new)
tests/unit/test_label_sessions.py # Unit tests for the labeling algorithm (new)
```

---

## Task 1: Implement the session labeling algorithm (pure function)

**Files:**
- Create: `tests/unit/test_label_sessions.py`
- Create: `ops/db/label_sessions.py`

- [ ] **Step 1.1: Write the failing tests**

Create `tests/unit/test_label_sessions.py`:

```python
import pytest

from ops.db.label_sessions import (
    InferredSession,
    assign_sessions,
)


def _row(ts, user_id="u1", user_agent="agent/1.0", model_id="m1", row_id=1):
    return {
        "id": row_id,
        "timestamp": ts,
        "user_id": user_id,
        "user_agent": user_agent,
        "model_id": model_id,
    }


class TestAssignSessionsBasic:
    def test_empty_input_returns_empty(self):
        assert assign_sessions([]) == []

    def test_single_request_gets_one_session(self):
        rows = [_row("2026-01-01T00:00:00Z")]
        result = assign_sessions(rows)
        assert len(result) == 1
        assert result[0].session_id.startswith("sess_")
        assert result[0].row_id == 1

    def test_two_close_requests_same_session(self):
        rows = [
            _row("2026-01-01T00:00:00Z", row_id=1),
            _row("2026-01-01T00:02:00Z", row_id=2),
        ]
        result = assign_sessions(rows)
        assert result[0].session_id == result[1].session_id

    def test_two_far_requests_different_session(self):
        rows = [
            _row("2026-01-01T00:00:00Z", row_id=1),
            _row("2026-01-01T00:15:00Z", row_id=2),
        ]
        result = assign_sessions(rows)
        assert result[0].session_id != result[1].session_id

    def test_different_user_agent_different_session(self):
        rows = [
            _row("2026-01-01T00:00:00Z", user_agent="agent-a", row_id=1),
            _row("2026-01-01T00:00:01Z", user_agent="agent-b", row_id=2),
        ]
        result = assign_sessions(rows)
        assert result[0].session_id != result[1].session_id

    def test_model_switch_same_session(self):
        rows = [
            _row("2026-01-01T00:00:00Z", model_id="opus", row_id=1),
            _row("2026-01-01T00:00:05Z", model_id="sonnet", row_id=2),
        ]
        result = assign_sessions(rows)
        assert result[0].session_id == result[1].session_id

    def test_gap_exactly_at_threshold_splits(self):
        rows = [
            _row("2026-01-01T00:00:00Z", row_id=1),
            _row("2026-01-01T00:10:00Z", row_id=2),
        ]
        result = assign_sessions(rows, gap_minutes=10.0)
        assert result[0].session_id != result[1].session_id

    def test_gap_just_under_threshold_same_session(self):
        rows = [
            _row("2026-01-01T00:00:00Z", row_id=1),
            _row("2026-01-01T00:09:59Z", row_id=2),
        ]
        result = assign_sessions(rows, gap_minutes=10.0)
        assert result[0].session_id == result[1].session_id

    def test_custom_gap_threshold(self):
        rows = [
            _row("2026-01-01T00:00:00Z", row_id=1),
            _row("2026-01-01T00:02:00Z", row_id=2),
        ]
        result = assign_sessions(rows, gap_minutes=1.0)
        assert result[0].session_id != result[1].session_id


class TestAssignSessionsMultiUser:
    def test_different_users_different_sessions(self):
        rows = [
            _row("2026-01-01T00:00:00Z", user_id="u1", row_id=1),
            _row("2026-01-01T00:00:01Z", user_id="u2", row_id=2),
        ]
        result = assign_sessions(rows)
        assert result[0].session_id != result[1].session_id

    def test_three_sessions_two_users(self):
        rows = [
            _row("2026-01-01T00:00:00Z", user_id="u1", row_id=1),
            _row("2026-01-01T00:01:00Z", user_id="u1", row_id=2),
            _row("2026-01-01T00:00:00Z", user_id="u2", row_id=3),
            _row("2026-01-01T01:00:00Z", user_id="u1", row_id=4),
        ]
        result = assign_sessions(rows)
        sess_ids = [r.session_id for r in result]
        assert sess_ids[0] == sess_ids[1]
        assert sess_ids[0] != sess_ids[2]
        assert sess_ids[0] != sess_ids[3]
        assert sess_ids[2] != sess_ids[3]

    def test_preserves_all_rows(self):
        rows = [
            _row("2026-01-01T00:00:00Z", user_id="u1", row_id=i)
            for i in range(100)
        ]
        result = assign_sessions(rows)
        assert len(result) == 100
        returned_ids = {r.row_id for r in result}
        assert returned_ids == set(range(100))


class TestSessionIdFormat:
    def test_session_id_format(self):
        rows = [_row("2026-01-01T00:00:00Z")]
        result = assign_sessions(rows)
        sid = result[0].session_id
        assert sid.startswith("sess_")
        parts = sid.split("_")
        assert len(parts) == 3

    def test_session_ids_sequential_within_group(self):
        rows = [
            _row("2026-01-01T00:00:00Z", row_id=1),
            _row("2026-01-01T00:15:00Z", row_id=2),
            _row("2026-01-01T00:30:00Z", row_id=3),
        ]
        result = assign_sessions(rows)
        seqs = [int(r.session_id.split("_")[-1]) for r in result]
        assert seqs == [1, 2, 3]

    def test_session_ids_sequential_across_groups(self):
        rows = [
            _row("2026-01-01T00:00:00Z", user_id="u1", row_id=1),
            _row("2026-01-01T00:00:00Z", user_id="u2", row_id=2),
            _row("2026-01-01T00:15:00Z", user_id="u1", row_id=3),
            _row("2026-01-01T00:15:00Z", user_id="u2", row_id=4),
        ]
        result = assign_sessions(rows)
        u1_sessions = [r for r in result if r.row_id in (1, 3)]
        u2_sessions = [r for r in result if r.row_id in (2, 4)]
        assert int(u1_sessions[0].session_id.split("_")[-1]) == 1
        assert int(u1_sessions[1].session_id.split("_")[-1]) == 2
        assert int(u2_sessions[0].session_id.split("_")[-1]) == 1
        assert int(u2_sessions[1].session_id.split("_")[-1]) == 2
```

- [ ] **Step 1.2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_label_sessions.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'ops.db.label_sessions'`

- [ ] **Step 1.3: Implement the labeling algorithm**

Create `ops/db/label_sessions.py` with the core `assign_sessions` function and `InferredSession` dataclass. The function takes a list of row dicts (each with keys `id`, `timestamp`, `user_id`, `user_agent`, `model_id`) and returns a list of `InferredSession` objects.

```python
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class InferredSession:
    row_id: int
    session_id: str
    user_id: str
    user_agent: str
    model_id: str
    timestamp: datetime


def _parse_ts(val: str | datetime) -> datetime:
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(val.replace("Z", "+00:00"))


def _group_hash(user_id: str, user_agent: str) -> str:
    raw = f"{user_id}\0{user_agent}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def assign_sessions(
    rows: list[dict[str, Any]],
    gap_minutes: float = 10.0,
) -> list[InferredSession]:
    if not rows:
        return []

    gap_threshold = timedelta(minutes=gap_minutes)
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for r in rows:
        parsed.append((_parse_ts(r["timestamp"]), r))

    parsed.sort(key=lambda p: (p[1].get("user_id", ""), p[1].get("user_agent", ""), p[0]))

    results: list[InferredSession] = []
    counters: dict[str, int] = {}
    prev_key: tuple[str, str] | None = None
    prev_ts: datetime | None = None

    for ts, row in parsed:
        uid = row.get("user_id") or ""
        ua = row.get("user_agent") or ""
        key = (uid, ua)
        gh = _group_hash(uid, ua)

        if key != prev_key or prev_ts is None or (ts - prev_ts) >= gap_threshold:
            counters[gh] = counters.get(gh, 0) + 1
            prev_ts = ts
        prev_key = key
        prev_ts = ts

        seq = counters.get(gh, 1)
        sid = f"sess_{gh}_{seq:03d}"

        results.append(
            InferredSession(
                row_id=row["id"],
                session_id=sid,
                user_id=uid,
                user_agent=ua,
                model_id=row.get("model_id", ""),
                timestamp=ts,
            )
        )

    return results
```

- [ ] **Step 1.4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_label_sessions.py -v`

Expected: All 15 tests PASS.

- [ ] **Step 1.5: Commit**

```bash
git add ops/db/label_sessions.py tests/unit/test_label_sessions.py
git commit -m "feat: add session labeling algorithm with gap-based heuristic"
```

---

## Task 2: Add database read/write and CLI entrypoint

**Files:**
- Modify: `ops/db/label_sessions.py`

- [ ] **Step 2.1: Write failing test for DB write logic**

Append to `tests/unit/test_label_sessions.py`:

```python
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops.db.label_sessions import main


@pytest.fixture
def mock_pool():
    conn = AsyncMock()
    conn.fetch.return_value = [
        {
            "id": 1,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "user_id": "u1",
            "user_agent": "agent/1.0",
            "model_id": "m1",
        },
        {
            "id": 2,
            "timestamp": "2026-01-01T00:01:00+00:00",
            "user_id": "u1",
            "user_agent": "agent/1.0",
            "model_id": "m1",
        },
    ]
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


class TestMainDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_write(self, mock_pool):
        pool, conn = mock_pool
        with patch("ops.db.label_sessions.asyncpg.create_pool", return_value=pool):
            await main(dry_run=True, gap_minutes=10.0)
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_mode_updates_db(self, mock_pool):
        pool, conn = mock_pool
        with patch("ops.db.label_sessions.asyncpg.create_pool", return_value=pool):
            await main(dry_run=False, gap_minutes=10.0)
        assert conn.execute.call_count == 2


class TestMainSummary:
    @pytest.mark.asyncio
    async def test_summary_output(self, mock_pool, capsys):
        pool, conn = mock_pool
        with patch("ops.db.label_sessions.asyncpg.create_pool", return_value=pool):
            await main(dry_run=True, gap_minutes=10.0)
        captured = capsys.readouterr()
        assert "Session labeling summary" in captured.out
        assert "Total requests" in captured.out
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_label_sessions.py::TestMainDryRun tests/unit/test_label_sessions.py::TestMainSummary -v`

Expected: FAIL — `ImportError: cannot import name 'main' from 'ops.db.label_sessions'`

- [ ] **Step 2.3: Add DB read/write functions and CLI to `ops/db/label_sessions.py`**

Append the following to `ops/db/label_sessions.py` (after the existing `assign_sessions` function):

```python
import argparse
import asyncio
import os
import sys

import asyncpg


async def _fetch_logs(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, timestamp, user_id,
                   metadata->>'user_agent' AS user_agent,
                   model_id
            FROM api_logs
            ORDER BY id
            """
        )
    return [dict(r) for r in rows]


async def _write_sessions(
    pool: asyncpg.Pool, sessions: list[InferredSession], batch_size: int = 500
) -> int:
    written = 0
    async with pool.acquire() as conn:
        for i in range(0, len(sessions), batch_size):
            batch = sessions[i : i + batch_size]
            await conn.executemany(
                "UPDATE api_logs SET session_id = $2 WHERE id = $1",
                [(s.row_id, s.session_id) for s in batch],
            )
            written += len(batch)
    return written


def _print_summary(sessions: list[InferredSession], dry_run: bool) -> None:
    mode = "DRY RUN" if dry_run else "WRITE"
    total = len(sessions)
    unique_sessions = len({s.session_id for s in sessions})
    unique_users = len({s.user_id for s in sessions})
    unique_agents = len({s.user_agent for s in sessions})

    sess_counts: dict[str, int] = {}
    for s in sessions:
        sess_counts[s.session_id] = sess_counts.get(s.session_id, 0) + 1

    sizes = list(sess_counts.values())

    print(f"\n{'=' * 60}")
    print(f"Session labeling summary ({mode})")
    print(f"{'=' * 60}")
    print(f"  Total requests:      {total}")
    print(f"  Unique sessions:     {unique_sessions}")
    print(f"  Unique users:        {unique_users}")
    print(f"  Unique user agents:  {unique_agents}")
    if sizes:
        print(f"  Session size min:    {min(sizes)}")
        print(f"  Session size max:    {max(sizes)}")
        print(f"  Session size avg:    {sum(sizes) / len(sizes):.1f}")
    print(f"{'=' * 60}\n")


async def main(
    dry_run: bool = True,
    gap_minutes: float = 10.0,
    batch_size: int = 500,
) -> None:
    dsn = (
        f"postgresql://{os.environ.get('DB_USER', 'postgres')}"
        f":{os.environ.get('DB_PASSWORD', '')}"
        f"@{os.environ.get('DB_HOST', 'localhost')}"
        f":{os.environ.get('DB_PORT', '5432')}"
        f"/{os.environ.get('DB_NAME', 'freeinference_db')}"
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        rows = await _fetch_logs(pool)
        if not rows:
            print("No rows found in api_logs.")
            return

        sessions = assign_sessions(rows, gap_minutes=gap_minutes)
        _print_summary(sessions, dry_run)

        if not dry_run:
            written = await _write_sessions(pool, sessions, batch_size)
            print(f"Wrote {written} session IDs to api_logs.session_id")
        else:
            print("Dry run — no rows updated. Use --write to persist.")
    finally:
        await pool.close()


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Label api_logs rows with inferred session IDs"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        default=False,
        help="Actually write session_id to the database (default: dry run)",
    )
    parser.add_argument(
        "--gap-minutes",
        type=float,
        default=10.0,
        help="Inactivity gap in minutes to split sessions (default: 10)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Batch size for UPDATE writes (default: 500)",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=not args.write, gap_minutes=args.gap_minutes, batch_size=args.batch_size))


if __name__ == "__main__":
    cli()
```

Also add the missing imports at the top of the file. The complete top of the file should have:

```python
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
```

Remove duplicate imports from the earlier section of the file if needed.

- [ ] **Step 2.4: Run all tests to verify they pass**

Run: `uv run pytest tests/unit/test_label_sessions.py -v`

Expected: All 18 tests PASS.

- [ ] **Step 2.5: Commit**

```bash
git add ops/db/label_sessions.py tests/unit/test_label_sessions.py
git commit -m "feat: add DB read/write and CLI to session labeling script"
```

---

## Task 3: Run the script in dry-run mode against the local database

**Files:**
- None (verification only)

- [ ] **Step 3.1: Run the script in dry-run mode**

```bash
source .venv/bin/activate
python -m ops.db.label_sessions --gap-minutes 10
```

Expected: Prints a summary showing ~6385 total requests, number of inferred sessions, session size stats. No rows updated.

- [ ] **Step 3.2: Verify no rows were modified**

```bash
docker exec hybridinference-postgres psql -U murphy -d freeinference_db -c \
  "SELECT COUNT(*) FROM api_logs WHERE session_id IS NOT NULL;"
```

Expected: `0` (dry run should not have written anything).

- [ ] **Step 3.3: Review the summary for reasonableness**

Check:
- Total requests matches `SELECT COUNT(*) FROM api_logs` (~6385)
- Number of sessions seems reasonable (should be much less than requests, probably 30-100 range given the data)
- Session size stats make sense (min >= 1, max probably < 2000)

- [ ] **Step 3.4: Commit** (only if any fixes were needed)

---

## Task 4: Run the script with `--write` and verify the database

**Files:**
- None (verification only)

- [ ] **Step 4.1: Run the script with --write**

```bash
python -m ops.db.label_sessions --write --gap-minutes 10
```

Expected: Summary printed, followed by "Wrote N session IDs to api_logs.session_id".

- [ ] **Step 4.2: Verify session IDs were written**

```bash
docker exec hybridinference-postgres psql -U murphy -d freeinference_db -c \
  "SELECT session_id, COUNT(*) FROM api_logs GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 20;"
```

Expected: Each session_id starts with `sess_`, counts vary, top sessions have the most requests.

- [ ] **Step 4.3: Verify session boundaries look correct**

Pick a session and check that requests within it are temporally close:

```bash
docker exec hybridinference-postgres psql -U murphy -d freeinference_db -c \
  "SELECT session_id, MIN(timestamp), MAX(timestamp), COUNT(*)
   FROM api_logs
   WHERE session_id IS NOT NULL
   GROUP BY session_id
   ORDER BY MIN(timestamp)
   LIMIT 20;"
```

Expected: Session spans (max - min) are typically under a few hours. Adjacent sessions for the same user should have a gap >= 10 minutes between the end of one and start of the next.

- [ ] **Step 4.4: Spot-check that model switches stay in the same session**

```bash
docker exec hybridinference-postgres psql -U murphy -d freeinference_db -c \
  "SELECT session_id, COUNT(DISTINCT model_id) as models, COUNT(*) as reqs
   FROM api_logs
   GROUP BY session_id
   HAVING COUNT(DISTINCT model_id) > 1
   ORDER BY reqs DESC
   LIMIT 10;"
```

Expected: Some sessions have multiple distinct models (confirming that model switches don't cause session splits).

- [ ] **Step 4.5: Commit** (only if any adjustments were made)

---

## Self-Review Checklist

1. **Spec coverage:** The script labels all requests with session IDs using temporal clustering. Dry-run is default. Write mode is opt-in. Summary report is printed. All requirements covered.

2. **Placeholder scan:** No TBD/TODO/placeholders. All code blocks contain complete implementation.

3. **Type consistency:** `InferredSession` fields (`row_id: int`, `session_id: str`, `user_id: str`, `user_agent: str`, `model_id: str`, `timestamp: datetime`) are used consistently across `assign_sessions`, `_write_sessions`, `_print_summary`, and tests.

4. **Edge cases covered by tests:** empty input, single request, gap exactly at threshold, gap just under threshold, model switching, different users, different user agents, custom gap threshold, large input (100 rows), dry-run vs write mode.
