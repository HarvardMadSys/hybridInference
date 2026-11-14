"""Inspect the shared spend accounting: ``python -m serving.shared_spend status | list``.

Read-only. ``status`` shows the stored mode, which registered processes report
which mode, the reservations still open (by instance and state: active,
unresolved, quarantined) and how many counter writes were made outside the
shared settlement while the mode was ``shared``. ``list`` shows the open
reservations themselves, oldest first, without account names.

There is deliberately no command that changes the mode or retires a
reservation. Activating or deactivating the shared mode is a controlled,
offline transition that still needs its own reviewed procedure (see the
implementation plan): processes cache the mode at startup, so changing it
while they run would mix the two accountings. A reservation is retired only by
its owner's settlement; an unresolved or quarantined one stays counted until a
separately reviewed reconciliation, with evidence of its actual charge,
exists.

Reads the same ``DB_*`` settings as the gateway; never prints a secret.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any

from serving.shared_spend.mode import CAPABILITY, PROTOCOL_VERSION

if TYPE_CHECKING:
    from collections.abc import Sequence

    from serving.shared_spend.store import SharedSpendStore


def participation(instances: list[dict[str, Any]], stored_mode: str) -> dict[str, Any]:
    """Which recently heard-from processes support and run the shared mode."""
    rows = []
    for instance in instances:
        if instance["stale"] or instance["lifecycle"] == "stopped":
            continue
        capability = (instance.get("capabilities") or {}).get(CAPABILITY) or {}
        rows.append(
            {
                "instance_id": instance["instance_id"],
                "build": instance["build"],
                "lifecycle": instance["lifecycle"],
                "protocol": capability.get("protocol"),
                "mode": capability.get("mode"),
            }
        )
    return {
        "instances": rows,
        "unsupported": [r["instance_id"] for r in rows if (r["protocol"] or 0) < PROTOCOL_VERSION],
        "not_in_stored_mode": [r["instance_id"] for r in rows if r["mode"] != stored_mode],
        # Registration cannot prove coverage: an older build never registers.
        "participation_proven": False,
    }


async def run(args: argparse.Namespace, backend: SharedSpendStore) -> list[str]:
    """Run one parsed command; return the lines to print."""
    from serving.config.instances import fetch_instances

    async with backend.pool.acquire() as conn:
        instances = await fetch_instances(conn)
    if args.command == "status":
        stored = await backend.read_mode()
        summary = await backend.summary()
        body = {
            "mode": stored,
            **participation(instances, stored),
            "open_reservations": summary["reservations"],
            "unidentified_writes": summary["unidentified_writes"],
            "last_unidentified_write": summary["last_unidentified_write"],
        }
        return [json.dumps(body, default=str, indent=2)]
    by_id = {i["instance_id"]: i for i in instances}
    lines = []
    for row in await backend.open_reservations(limit=args.limit):
        owner = by_id.get(row["instance_id"])
        row["owner"] = (
            "not registered"
            if owner is None
            else ("stale" if owner["stale"] else owner["lifecycle"])
        )
        lines.append(json.dumps(row, default=str))
    return lines or ["No open reservations."]


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m serving.shared_spend",
        description="Inspect the shared spend accounting (read-only).",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("status", help="mode, participating processes, open reservations")
    listing = commands.add_parser("list", help="list open reservations")
    listing.add_argument("--limit", type=int, default=200)
    return parser


async def _main(args: argparse.Namespace) -> list[str]:
    import asyncpg

    from serving.config.settings import get_settings
    from serving.shared_spend.store import SharedSpendStore

    settings = get_settings()
    pool = await asyncpg.create_pool(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        min_size=1,
        max_size=2,
    )
    try:
        return await run(args, SharedSpendStore(pool))
    finally:
        await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command named on the command line; 0 on success, 1 when it failed."""
    args = build_parser().parse_args(argv)
    try:
        lines = asyncio.run(_main(args))
    except (OSError, ConnectionError) as exc:
        print(f"Could not use the database: {type(exc).__name__}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
