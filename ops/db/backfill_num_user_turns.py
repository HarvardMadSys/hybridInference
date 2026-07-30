"""Backfill ``api_logs.num_user_turns`` after the genuine-user-turn fix.

Historical rows were written by a ``conversation_shape`` that counted every
``role == "user"`` message as a user turn. Coding agents (Claude Code
especially) inject many user-role messages the human never typed -- harness
context/reminders wrapped in tags like ``<system-reminder>`` /
``<task-notification>`` / ``<environment_details>``, and Anthropic tool results
carried as user-role ``tool_result`` messages -- so a long resent history
reported hundreds of phantom "user turns" in the admin dashboard. The fix (see
``serving.storage.utils.conversation_shape`` / ``_is_genuine_user_turn``)
excludes those injected messages.

This script recomputes ``num_user_turns`` from the stored ``prompt`` using the
corrected function and updates rows whose value changed. It imports the live
``conversation_shape``, so it always matches current write-time behavior and
stays in sync as the injected-wrapper set evolves.

Scope: every row with a retained ``prompt`` is scanned (no prefilter), so the
recompute exactly tracks the detector regardless of which wrappers a row uses.
Rows whose prompt was not retained (content storage disabled) cannot be
recomputed and are left as-is.

Idempotent: after a full ``--apply`` pass, re-running reports zero changes.
Dry-run by default -- pass ``--apply`` to write.

Usage:
    python ops/db/backfill_num_user_turns.py                 # dry run, report only
    python ops/db/backfill_num_user_turns.py --apply         # write changes
    python ops/db/backfill_num_user_turns.py --batch-size 2000 --apply
    python ops/db/backfill_num_user_turns.py --env-file /srv/hybridInference/.env --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
import dotenv

# Make the ``serving`` package importable when running from a source checkout
# without an editable install (apps/backend is the package root).
_BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
if _BACKEND_ROOT.is_dir() and str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from serving.storage.utils import conversation_shape


def _load_env(env_path: str | None = None) -> None:
    candidates = [
        env_path,
        os.environ.get("ENV_FILE"),
        "/srv/hybridInference/.env",
        str(Path(__file__).resolve().parents[2] / ".env"),
    ]
    for p in candidates:
        if p and Path(p).is_file():
            dotenv.load_dotenv(p, override=False)
            return


def _dsn() -> str:
    user = os.environ.get("DB_USER")
    if not user:
        raise SystemExit(
            "ERROR: DB_USER is not set. Load the .env file or set the environment variable."
        )
    return (
        f"postgresql://{user}:{os.environ.get('DB_PASSWORD', '')}"
        f"@{os.environ.get('DB_HOST', 'localhost')}:{os.environ.get('DB_PORT', '5432')}"
        f"/{os.environ.get('DB_NAME', 'hybridinference')}"
    )


def _recompute_user_turns(prompt_text: str) -> int | None:
    """Return the corrected ``num_user_turns`` for a stored prompt, or None.

    Returns None when the prompt is not valid JSON, not a chat-style messages
    list, or otherwise yields no turn count -- callers skip those rows rather
    than overwrite a value with NULL.
    """
    try:
        parsed = json.loads(prompt_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    _num_turns, num_user_turns, _num_tool_calls = conversation_shape(parsed)
    return num_user_turns


async def _run(dsn: str, *, apply: bool, batch_size: int, progress_every: int) -> int:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    scanned = 0
    changed = 0
    parse_skipped = 0
    increased = 0  # rows where recompute > stored (unexpected for this fix)
    sum_old = 0
    sum_new = 0
    last_id = 0
    batches = 0
    try:
        async with pool.acquire() as conn:
            total = await conn.fetchval("SELECT count(*) FROM api_logs WHERE prompt IS NOT NULL")
            print(
                f"Candidate rows (prompt retained): {total:,}\n"
                f"Mode: {'APPLY (writing changes)' if apply else 'DRY RUN (no writes)'}\n"
            )
            while True:
                rows = await conn.fetch(
                    "SELECT id, prompt, num_user_turns FROM api_logs "
                    "WHERE prompt IS NOT NULL AND id > $1 "
                    "ORDER BY id LIMIT $2",
                    last_id,
                    batch_size,
                )
                if not rows:
                    break
                batches += 1
                last_id = rows[-1]["id"]
                updates: list[tuple[int, int]] = []  # (new_value, id)
                for r in rows:
                    scanned += 1
                    new_val = _recompute_user_turns(r["prompt"])
                    if new_val is None:
                        parse_skipped += 1
                        continue
                    old_val = r["num_user_turns"]
                    if old_val == new_val:
                        continue
                    changed += 1
                    sum_old += old_val or 0
                    sum_new += new_val
                    if old_val is not None and new_val > old_val:
                        increased += 1
                    updates.append((new_val, r["id"]))
                if apply and updates:
                    async with conn.transaction():
                        await conn.executemany(
                            "UPDATE api_logs SET num_user_turns = $1 WHERE id = $2",
                            updates,
                        )
                if progress_every and batches % progress_every == 0:
                    print(f"  ...scanned {scanned:,} / changed {changed:,} (last id {last_id:,})")
    finally:
        await pool.close()

    print(
        "\n=== Backfill summary ==="
        f"\n  scanned:        {scanned:,}"
        f"\n  changed:        {changed:,}"
        f"\n  unchanged:      {scanned - changed - parse_skipped:,}"
        f"\n  parse-skipped:  {parse_skipped:,}"
        f"\n  increased(!):   {increased:,}   (recompute > stored; expected 0 for this fix)"
        f"\n  sum num_user_turns over changed rows: {sum_old:,} -> {sum_new:,} "
        f"(removed {sum_old - sum_new:,} phantom user turns)"
    )
    if not apply:
        print("\nDRY RUN -- no rows were modified. Re-run with --apply to write.")
    else:
        print("\nAPPLIED -- num_user_turns updated for the changed rows.")
    return 0


def cli() -> None:
    """Parse CLI args, load env, and run the backfill."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the recomputed values. Without this flag the script is a dry run.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=1000, help="Rows per keyset page (default: 1000)"
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=20,
        help="Print progress every N batches (0 to disable; default: 20)",
    )
    parser.add_argument("--env-file", default=None, help="Path to .env (default: auto-detect)")
    args = parser.parse_args()
    _load_env(args.env_file)
    raise SystemExit(
        asyncio.run(
            _run(
                _dsn(),
                apply=args.apply,
                batch_size=args.batch_size,
                progress_every=args.progress_every,
            )
        )
    )


if __name__ == "__main__":
    cli()
