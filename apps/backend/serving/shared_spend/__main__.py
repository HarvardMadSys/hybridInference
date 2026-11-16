"""Inspect and reconcile the shared spend accounting: ``python -m serving.shared_spend COMMAND``.

``status`` shows the stored mode, which registered processes report which
mode, the reservations still open (by instance and state: active or
quarantined, with how their owner is known), the settlement receipts kept and
how old the oldest is, the closures recorded, what operators have reconciled,
and how many counter writes were made outside the shared settlement while the
mode was ``shared``. Its ``stranded`` entry adds up the active reservations
whose owner may never settle them (closed, stopped, stale, or not registered):
a process that was killed leaves these, and they keep counting against their
account's day and month until they are reconciled. ``list`` shows the open
reservations themselves, oldest first. Neither names an account.

``reconcile INSTANCE_ID`` retires the active reservations a process that is
gone left behind (:mod:`serving.shared_spend.reconcile`). Without ``--apply``
it only shows what it would do: for each reservation, the request-log rows
found for it and what it would be charged, or that it would be given back
whole. With ``--apply --operator NAME --reason TEXT`` it does it, one audit
row per reservation, and records the instance closed. It refuses an instance
the registry still hears from. The registry cannot prove a process gone:
running this is the operator's statement that they checked.

There is deliberately no command that changes the mode. Activating or
deactivating the shared mode is a controlled, offline transition that still
needs its own reviewed procedure (see the implementation plan): processes
cache the mode at startup, so changing it while they run would mix the two
accountings. Quarantined reservations stay counted: the counters refused their
costs, which no command here settles.

Reads the same ``DB_*`` settings as the gateway; never prints a secret.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any

from serving.shared_spend import reconcile
from serving.shared_spend.mode import CAPABILITY, PROTOCOL_VERSION
from serving.shared_spend.stats import stranded
from serving.shared_spend.store import OwnerHeardFrom

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


class Refused(Exception):
    """The command was asked for something it will not do; the message says what."""


async def run(args: argparse.Namespace, backend: SharedSpendStore) -> list[str]:
    """Run one parsed command; return the lines to print.

    Raises:
        Refused: ``reconcile --apply`` named an instance the registry hears
            from, or one nothing names, or came without who and why.
    """
    if args.command == "status":
        from serving.config.instances import fetch_instances

        async with backend.pool.acquire() as conn:
            instances = await fetch_instances(conn)
        stored = await backend.read_mode()
        summary = await backend.summary()
        body = {
            "mode": stored,
            **participation(instances, stored),
            "open_reservations": summary["reservations"],
            "stranded": stranded(summary["reservations"]),
            "receipts": summary["receipts"],
            "closures": summary["closures"],
            "reconciled": summary["reconciled"],
            "unidentified_writes": summary["unidentified_writes"],
            "last_unidentified_write": summary["last_unidentified_write"],
        }
        return [json.dumps(body, default=str, indent=2)]
    if args.command == "reconcile":
        if not args.apply:
            body = await reconcile.preview(backend, args.instance_id, limit=args.limit)
            return [json.dumps(body, default=str, indent=2)]
        operator = (args.operator or "").strip()
        reason = (args.reason or "").strip()
        if not operator or not reason:
            raise Refused("--apply needs --operator NAME and --reason TEXT: both are recorded")
        try:
            body = await reconcile.apply(
                backend, args.instance_id, operator=operator[:200], reason=reason[:1000]
            )
        except (OwnerHeardFrom, reconcile.UnknownInstance) as exc:
            raise Refused(str(exc)) from exc
        return [json.dumps(body, default=str, indent=2)]
    lines = [
        json.dumps(row, default=str) for row in await backend.open_reservations(limit=args.limit)
    ]
    return lines or ["No open reservations."]


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m serving.shared_spend",
        description="Inspect the shared spend accounting, and reconcile what a gone process left.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("status", help="mode, participating processes, open reservations")
    listing = commands.add_parser("list", help="list open reservations")
    listing.add_argument("--limit", type=int, default=200)
    reconciling = commands.add_parser(
        "reconcile",
        help="retire the reservations a process that is gone left (shows the plan unless --apply)",
    )
    reconciling.add_argument("instance_id", metavar="INSTANCE_ID")
    reconciling.add_argument(
        "--apply", action="store_true", help="do it; without this only the plan is shown"
    )
    reconciling.add_argument("--operator", help="who says the process is gone (recorded)")
    reconciling.add_argument("--reason", help="how they know, and why now (recorded)")
    reconciling.add_argument(
        "--limit", type=int, default=1000, help="reservations the plan shows at most"
    )
    return parser


async def _main(args: argparse.Namespace) -> list[str]:
    import asyncpg

    from serving.config.settings import get_settings
    from serving.shared_spend.store import SharedSpendStore

    settings = get_settings()
    fence_secret = _fence_secret(args)
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
        return await run(args, SharedSpendStore(pool, fence_secret=fence_secret))
    finally:
        await pool.close()


def _fence_secret(args: argparse.Namespace) -> str | None:
    """The erasure fence's secret for a command that writes; None for one that only reads.

    A reconciliation adds costs to the counters, so it runs under the fence
    exactly as the gateway's own settlements do (it must not write a counter
    for an account being hard-deleted), and is refused where the secret is not
    configured.
    """
    if args.command != "reconcile" or not args.apply:
        return None
    from serving.storage.erasure_fence import ErasureFenceUnavailable, resolve_fence_secret

    try:
        return resolve_fence_secret(None)
    except ErasureFenceUnavailable as exc:
        raise Refused(
            "reconcile --apply needs the gateway's erasure-fence secret "
            "(ERASURE_FENCE_SECRET, or API_KEY_SECRET) in its environment"
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command named on the command line; 0 on success, 1 when it failed or refused."""
    args = build_parser().parse_args(argv)
    try:
        lines = asyncio.run(_main(args))
    except Refused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    except (OSError, ConnectionError) as exc:
        print(f"Could not use the database: {type(exc).__name__}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
