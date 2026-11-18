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

**What a reservation is charged.** What its billing points had recorded
under it when its request was logged. A request's log row names the
reservation it ran under and that amount (``metadata.spend_reservation``,
``metadata.spend_charged_amount``): the ledger's own record of what the
reservation's settlement would have added to the counters. So a request that
ended, and was logged before its process died, is charged exactly what it
would have been. The row's own ``cost_amount`` is shown beside it and not used: a
failed request's row can carry a cost that no billing point charged. When a
reservation has several rows, each carries the total recorded when it was
written, and the largest is the charge.

**On which day.** The days its billing points recorded it on, which the row
carries too (``metadata.spend_charged_days``): the days its own settlement
would have counted it on. Not the day the row was written, which is later --
the next day, and at a month's end the next month, for a charge recorded just
before midnight. A row that does not carry them, or carries days that cannot
be its charge's, is counted on the day it was written.

A reservation with no log row, or whose rows record no charge -- its request
was cut off by the process dying, was charged nothing, or its row was never
written -- is charged nothing and its whole estimate is given back, which is
what a process dying mid-request costs a caller in the per-process accounting
too. The estimate itself is never charged.

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

from datetime import UTC, date, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from serving.accounting import get_accounting_policy
from serving.shared_spend.store import (
    OWNER_GONE,
    CostLine,
    Member,
    OwnerHeardFrom,
    Reconciliation,
    quantized_amount,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

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


def _recorded_charge(value: Any) -> Decimal | None:
    """The charge a log row records (``metadata.spend_charged_amount``), or None if it has none."""
    if value is None:
        return None
    try:
        charge = quantized_amount(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None
    return charge if charge >= 0 else None


def _recorded_days(
    value: Any, charge: Decimal, *, admitted: date, written: date
) -> list[tuple[str, Decimal, int]] | None:
    """The days a log row says its charge was recorded on, as (day, cost, requests); else None.

    ``metadata.spend_charged_days``. None for a row that carries none, and for
    days that cannot be its charge's: costs that do not add up to it, a day
    that is not one or is there twice, or one outside the reservation's life,
    from its admission to the writing of the row (a day either side, for
    clocks that disagree).
    """
    if not isinstance(value, list) or not value:
        return None
    days: list[tuple[str, Decimal, int]] = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        day, requests = entry.get("day"), entry.get("requests")
        cost = _recorded_charge(entry.get("amount"))
        if not isinstance(day, str) or not cost or type(requests) is not int or requests < 0:
            return None
        try:
            on = date.fromisoformat(day)
        except ValueError:
            return None
        if on.isoformat() != day or not (
            admitted - timedelta(days=1) <= on <= written + timedelta(days=1)
        ):
            return None
        days.append((day, cost, requests))
    if len({day for day, _, _ in days}) != len(days):
        return None
    if sum((cost for _, cost, _ in days), Decimal(0)) != charge:
        return None
    return days


async def plan(
    backend: SharedSpendStore,
    instance_id: str,
    *,
    limit: int = CHUNK,
    exclude: Sequence[str] = (),
) -> list[Reconciliation]:
    """What reconciling *instance_id*'s oldest active reservations would do, at most *limit*.

    Reads only. Each reservation is paired with the request-log rows written
    under it. Each row records what the billing points had charged the
    reservation when it was written, and on which UTC days; the largest of
    those is the charge (of equal ones, the row written last), counted on
    its days with the requests they counted: what the reservation's own
    settlement would have added. A row that does not say on which days is
    counted on the day it was written, as one request. Reservations in
    *exclude* are left out.
    """
    reservations = await backend.reservations_of(instance_id, limit=limit, exclude=exclude)
    logged = await backend.request_log_rows(reservations)
    planned = []
    for row in reservations:
        admitted = row["created_at"].astimezone(UTC).date()
        evidence: list[dict[str, Any]] = []
        charge, lines = Decimal(0), []
        for found in logged.get(row["reservation_id"], []):
            recorded = _recorded_charge(found["charged_amount"])
            days = None
            if recorded:
                days = _recorded_days(
                    found.get("charged_days"),
                    recorded,
                    admitted=admitted,
                    written=date.fromisoformat(found["day"]),
                )
            evidence.append(
                {
                    "request_id": found["request_id"],
                    "day": found["day"],
                    "status_code": found["status_code"],
                    "charged_amount": str(recorded) if recorded is not None else None,
                    # The days the row says the charge was recorded on, which
                    # it is counted on; None where it does not say.
                    "charged_days": {day: str(cost) for day, cost, _ in days} if days else None,
                    # The row's own price for the request, beside it for an
                    # operator to compare; not what is charged.
                    "cost_amount": str(found["cost_amount"])
                    if found["cost_amount"] is not None
                    else None,
                }
            )
            if recorded is not None and recorded >= charge:
                charge, lines = recorded, days or [(found["day"], recorded, 1)]
        costs = (
            tuple(
                CostLine(
                    user_id=row["user_id"],
                    day=day,
                    api_key_id=row["api_key_id"],
                    cost_amount=cost,
                    requests=requests,
                    project_id=row["project_id"],
                )
                for day, cost, requests in lines
            )
            if charge > 0
            else ()
        )
        planned.append(
            Reconciliation(
                reservation_id=row["reservation_id"],
                amount=quantized_amount(Decimal(row["amount"])),
                operation=row["operation"],
                admitted_at=row["created_at"],
                member=Member(
                    reservation_id=row["reservation_id"],
                    user_id=row["user_id"],
                    kind="settle",
                    api_key_id=row["api_key_id"],
                    project_id=row["project_id"],
                    costs=costs,
                ),
                evidence=tuple(evidence),
            )
        )
    return planned


def describe(entry: Reconciliation) -> dict[str, Any]:
    """One planned reservation as the command prints it; no account is named."""
    cost = entry.member.cost_amount()
    return {
        "reservation_id": entry.reservation_id,
        "held_amount": str(entry.amount),
        "currency": get_accounting_policy().currency,
        "operation": entry.operation,
        "admitted_at": entry.admitted_at,
        "would": "charge" if cost else "release",
        "charge_amount": str(cost),
        "charge_days": {line.day: str(line.cost_amount) for line in entry.member.costs},
        "request_log": list(entry.evidence),
    }


def totals(planned: list[Reconciliation]) -> dict[str, Any]:
    """What a plan adds up to."""
    charged = [entry for entry in planned if entry.member.costs]
    return {
        "reservations": len(planned),
        "held_amount": str(sum((entry.amount for entry in planned), Decimal(0))),
        "to_charge": len(charged),
        "charge_amount": str(sum((entry.member.cost_amount() for entry in charged), Decimal(0))),
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
        "active_total": held.get("active", 0),
        "quarantined_left": held.get("quarantined", 0),
        "plan": [describe(entry) for entry in planned],
    }


async def apply(
    backend: SharedSpendStore, instance_id: str, *, operator: str, reason: str
) -> dict[str, Any]:
    """Retire every active reservation of *instance_id* and record it closed.

    In chunks of :data:`CHUNK`, each planned from the request log and retired
    in one transaction with its audit rows
    (:meth:`~serving.shared_spend.store.SharedSpendStore.reconcile`). A chunk
    the counters refuse for a charge's own values is retired one reservation
    at a time, and a reservation refused alone is left as it is and reported
    (``not_retired["refused"]``): one charge the counters cannot take must not
    keep every other reservation of the instance held. The instance is then
    not recorded closed, and neither when a chunk retires nothing at all: what
    is left wants a look.

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
        "charge_amount": Decimal(0),
        "released": 0,
        "released_amount": Decimal(0),
        "not_retired": {},
        "batches": [],
    }
    from serving.spend_ledger import _refused

    refused: list[str] = []
    while True:
        planned = await plan(backend, instance_id, exclude=refused)
        if not planned:
            break
        results = []
        refused_before = len(refused)
        try:
            results.append(
                await backend.reconcile(instance_id, planned, operator=operator, reason=reason)
            )
        except Exception as exc:
            if not _refused(exc):
                raise
            # The counters refused a charge, which rolled the chunk back whole.
            for entry in planned:
                try:
                    results.append(
                        await backend.reconcile(
                            instance_id, [entry], operator=operator, reason=reason
                        )
                    )
                except Exception as alone:
                    if not _refused(alone):
                        raise
                    refused.append(entry.reservation_id)
        by_id = {entry.reservation_id: entry for entry in planned}
        retired = 0
        for result, _ in results:
            done["batches"].append(result.batch_id)
            for rid in result.members.get("settled", []):
                done["charged"] += 1
                done["charge_amount"] += by_id[rid].member.cost_amount()
                retired += 1
            for rid in result.members.get("released", []):
                done["released"] += 1
                done["released_amount"] += by_id[rid].amount
                retired += 1
            for outcome, ids in result.members.items():
                if outcome not in ("settled", "released"):
                    done["not_retired"].setdefault(outcome, []).extend(ids)
        if not retired and len(refused) == refused_before:
            done["stopped"] = "a chunk retired nothing; the instance was not closed"
            break
    if refused:
        done["not_retired"]["refused"] = refused
        done["stopped"] = (
            f"the counters refused the charge of {len(refused)} reservation(s), which stay "
            "held; the instance was not closed"
        )
    held = await backend.held_by(instance_id)
    done["quarantined_left"] = held.get("quarantined", 0)
    done["active_left"] = held.get("active", 0)
    if "stopped" not in done:
        done["closed_now"] = await backend.close_instance(
            instance_id, closed_by="operator", operator=operator, reason=reason
        )
    done["charge_amount"] = str(done["charge_amount"])
    done["released_amount"] = str(done["released_amount"])
    return done
