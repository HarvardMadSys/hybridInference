"""Retire what a gateway process that is gone left behind, on evidence and on record.

A process that was killed leaves its reservations ``active`` in the database.
Nothing settles them -- only their owner does, and it never will -- so they
keep counting against their accounts' current day and month. This is the
operator's way to retire them (``python -m serving.shared_spend reconcile``).

**Who may be reconciled.** Only an instance known as gone
(:data:`~serving.shared_spend.store.OWNER_GONE`): one that recorded its own
closure, or one the registry no longer hears from. The registry cannot prove a
process gone -- it may only be cut off from the database -- so nothing here
runs by itself: an operator who has checked says so, by name and with a
reason, and that statement is recorded. An instance the registry hears from is
refused; it retires its own.

**What a reservation is charged.** What the request log says its request
cost. A request's log row names the reservation it ran under and carries a
cost computed from the same usage and price as the charge its billing point
recorded, so a request that ended, and was logged before its process died, is
charged exactly that. A reservation with no log row -- its request was cut off
by the process dying, or its row was never written -- is charged nothing and
its whole estimate is given back, which is what a process dying mid-request
costs a caller in the per-process accounting too. The estimate itself is never
charged.

**What is recorded.** Each reservation retired gets a row in
``spend_reconciliations`` in the transaction that retires it: what it held,
what it was charged, the request-log rows that say so, how its owner was known
then, who ran this and why. The instance is recorded as closed, which is what
lets its settlement receipts be deleted in time.

A reconciliation is a settlement made in the gone process's name, so its
retirements are recorded like any other. Should the process turn out to be
alive and settle the same reservations later, its settlement finds them
retired and adds nothing: a wrong "it is gone" can leave requests uncharged,
never charge one twice.

Quarantined reservations are not touched: the counters refused their costs for
the values themselves, and what to do about that is not a question of who is
gone.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from serving.shared_spend.store import (
    OWNER_GONE,
    CostLine,
    Member,
    OwnerHeardFrom,
    Reconciliation,
    usd,
)

if TYPE_CHECKING:
    from serving.shared_spend.store import SharedSpendStore

#: Reservations planned and retired in one transaction (a settlement batch's bound).
CHUNK = 256


class UnknownInstance(LookupError):
    """Nothing names the instance a reconciliation was asked for."""

    def __init__(self, instance_id: str) -> None:
        """Keep the id that was asked for."""
        super().__init__(
            f"nothing names instance {instance_id}: no reservation, receipt, registration or "
            "closure (check the id with: python -m serving.shared_spend status)"
        )
        self.instance_id = instance_id


async def plan(
    backend: SharedSpendStore, instance_id: str, *, limit: int = CHUNK
) -> list[Reconciliation]:
    """What reconciling *instance_id*'s oldest active reservations would do, at most *limit*.

    Reads only. Each reservation is paired with the request-log rows written
    under it; a row with a cost becomes a charge on the UTC day the row was
    written, capped as every recorded cost is.
    """
    from serving.spend_ledger import MAX_REQUEST_COST_USD

    reservations = await backend.reservations_of(instance_id, limit=limit)
    logged = await backend.request_log_rows(reservations)
    ceiling = usd(MAX_REQUEST_COST_USD)
    planned = []
    for row in reservations:
        costs: list[CostLine] = []
        evidence: list[dict[str, Any]] = []
        for found in logged.get(row["reservation_id"], []):
            cost = usd(Decimal(found["cost_usd"] or 0))
            charged = min(cost, ceiling)
            evidence.append(
                {
                    "request_id": found["request_id"],
                    "day": found["day"],
                    "status_code": found["status_code"],
                    "cost_usd": str(cost),
                    "charged_usd": str(charged),
                }
            )
            if charged > 0:
                costs.append(
                    CostLine(
                        user_id=row["user_id"],
                        day=found["day"],
                        api_key_id=row["api_key_id"],
                        cost_usd=charged,
                        requests=1,
                        project_id=row["project_id"],
                    )
                )
        planned.append(
            Reconciliation(
                reservation_id=row["reservation_id"],
                amount_usd=Decimal(row["amount_usd"]),
                operation=row["operation"],
                admitted_at=row["created_at"],
                member=Member(
                    reservation_id=row["reservation_id"],
                    user_id=row["user_id"],
                    kind="settle",
                    api_key_id=row["api_key_id"],
                    project_id=row["project_id"],
                    costs=tuple(costs),
                ),
                evidence=tuple(evidence),
            )
        )
    return planned


def describe(entry: Reconciliation) -> dict[str, Any]:
    """One planned reservation as the command prints it; no account is named."""
    cost = entry.member.cost_usd()
    return {
        "reservation_id": entry.reservation_id,
        "held_usd": str(entry.amount_usd),
        "operation": entry.operation,
        "admitted_at": entry.admitted_at,
        "would": "charge" if cost else "release",
        "charge_usd": str(cost),
        "request_log": list(entry.evidence),
    }


def totals(planned: list[Reconciliation]) -> dict[str, Any]:
    """What a plan adds up to."""
    charged = [entry for entry in planned if entry.member.costs]
    return {
        "reservations": len(planned),
        "held_usd": str(sum((entry.amount_usd for entry in planned), Decimal(0))),
        "to_charge": len(charged),
        "charge_usd": str(sum((entry.member.cost_usd() for entry in charged), Decimal(0))),
        "to_release": len(planned) - len(charged),
    }


async def preview(
    backend: SharedSpendStore, instance_id: str, *, limit: int = 1000
) -> dict[str, Any]:
    """What ``reconcile --apply`` would do for *instance_id*; changes nothing."""
    owner = (await backend.owner_states()).get(instance_id, "not registered")
    known = await backend.instance_known(instance_id)
    held = await backend.held_by(instance_id)
    planned = await plan(backend, instance_id, limit=limit)
    return {
        "instance_id": instance_id,
        "known": known,
        "owner": owner,
        "reconcilable": known and owner in OWNER_GONE,
        "applied": False,
        **totals(planned),
        "active_in_all": held.get("active", 0),
        "quarantined_left": held.get("quarantined", 0),
        "plan": [describe(entry) for entry in planned],
    }


async def apply(
    backend: SharedSpendStore, instance_id: str, *, operator: str, reason: str
) -> dict[str, Any]:
    """Retire every active reservation of *instance_id* and record it closed.

    In chunks of :data:`CHUNK`, each planned from the request log and retired
    in one transaction with its audit rows
    (:meth:`~serving.shared_spend.store.SharedSpendStore.reconcile`). Stops,
    without closing the instance, when a chunk retires nothing: what is left
    is no longer held as planned and wants a look.

    Raises:
        OwnerHeardFrom: The registry hears from the instance; nothing is done.
        UnknownInstance: Nothing names the instance (a mistyped id, most
            likely); nothing is done, and no closure is recorded for it.
    """
    owner = (await backend.owner_states()).get(instance_id, "not registered")
    if owner not in OWNER_GONE:
        raise OwnerHeardFrom(instance_id, owner)
    if not await backend.instance_known(instance_id):
        raise UnknownInstance(instance_id)
    done: dict[str, Any] = {
        "instance_id": instance_id,
        "owner": owner,
        "applied": True,
        "charged": 0,
        "charge_usd": Decimal(0),
        "released": 0,
        "released_usd": Decimal(0),
        "not_retired": {},
        "batches": [],
    }
    while True:
        planned = await plan(backend, instance_id)
        if not planned:
            break
        result, _ = await backend.reconcile(instance_id, planned, operator=operator, reason=reason)
        by_id = {entry.reservation_id: entry for entry in planned}
        done["batches"].append(result.batch_id)
        for rid in result.members.get("settled", []):
            done["charged"] += 1
            done["charge_usd"] += by_id[rid].member.cost_usd()
        for rid in result.members.get("released", []):
            done["released"] += 1
            done["released_usd"] += by_id[rid].amount_usd
        for outcome, ids in result.members.items():
            if outcome not in ("settled", "released"):
                done["not_retired"].setdefault(outcome, []).extend(ids)
        if not (result.members.get("settled") or result.members.get("released")):
            done["stopped"] = "a chunk retired nothing; the instance was not closed"
            break
    held = await backend.held_by(instance_id)
    done["quarantined_left"] = held.get("quarantined", 0)
    done["active_left"] = held.get("active", 0)
    if "stopped" not in done:
        done["closed_now"] = await backend.close_instance(
            instance_id, closed_by="operator", operator=operator, reason=reason
        )
    done["charge_usd"] = str(done["charge_usd"])
    done["released_usd"] = str(done["released_usd"])
    return done
