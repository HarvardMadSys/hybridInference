"""The SQL of shared spend accounting: admission, settlement, and a read-only status view.

Every statement here runs on tables the schema migrations create
(``0019_shared_spend``); nothing issues DDL. See
``docs/agents/plans/2026-11-14-shared-spend-admission-and-settlement.md`` for
the protocol; the invariants each function keeps are stated where it keeps
them.

**Locks.** Admission takes, in one fixed order, the account's shared
erasure-fence lock (``erasure_fence``'s 64-bit key space) and then the
exclusive transaction-level advisory lock of every scope it checks, in the
two-key space (``(class, key)``, which never overlaps the 64-bit one), sorted.
Settlement takes only the fence locks, shared, as the cost writer always has.
Nothing waits for a lock while holding a lock of a later class, so these cannot
deadlock one another.

**Reads.** Admission reads the counters and the open reservations in *one*
statement issued after the statement that took its locks returned: under READ
COMMITTED that statement's snapshot is taken after every lock was granted, so
it sees every admission that held those locks before, and a settlement --
which moves a cost from a reservation to a counter in one commit -- is in it
wholly or not at all. Reading the two in separate statements would let a
settlement commit between them and drop its cost from both.

**Receipts.** A settlement records its batch (id and payload digest) and,
for each reservation it retires, a member row keyed by the reservation id.
They are what makes a repeated batch harmless and a retired reservation id
unusable, so one may go only when nothing can send its batch again -- and a
batch is only ever sent again by the process that made it, until that process
has its answer. :meth:`SharedSpendStore.purge_receipts` deletes receipts older
than :data:`RECEIPT_RETENTION_SECONDS` under exactly that rule: a process's
own, except those of batches it has not been answered for, and those of a
process ``spend_instance_closures`` says closed that long ago. Age alone never
deletes one.

**Retirement.** Nothing here retires a reservation except its owner's
settlement and, for a process that is gone, an operator's reconciliation
(:meth:`SharedSpendStore.reconcile`), which is a settlement made in that
process's name with an audit row for every reservation it retires.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Literal

from serving.storage.erasure_fence import (
    ERASURE_FENCE_TABLE,
    fence_account_advisory_key,
    fence_account_digest,
    lock_fence_keys_shared,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

#: Advisory lock classes of the scopes an admission checks (two-key space).
LOCK_CLASS_ACCOUNT = 0x53500001
LOCK_CLASS_KEY = 0x53500002
LOCK_CLASS_PROJECT = 0x53500003

#: The setting a shared settlement marks its transaction with, so the counter
#: tables' trigger knows the write as one of its own (``0019_shared_spend``).
WRITER_SETTING = "hybridinference.spend_writer"

#: More accounts than this in one settlement check the fence in the counter
#: statement alone rather than lock each, as the cost writer does.
FENCE_LOCKS_MAX = 512

#: How long a settlement receipt is kept once nothing can send its batch again,
#: and how long after a process closed its receipts are kept.
RECEIPT_RETENTION_SECONDS = 7 * 24 * 3600.0

#: The most batch receipts one purge statement deletes (with their members).
PURGE_BATCHES = 500

#: How the registry or a closure knows a reservation's owner when that owner
#: may never settle what it holds: closed (it said so, or an operator did),
#: stopped, not heard from for too long, or no longer registered at all.
OWNER_GONE = frozenset({"closed", "stopped", "stale", "not registered"})

_MICRO = Decimal("0.000001")


def _hash32(text: str) -> int:
    """A signed 32-bit key for *text*: collisions only serialize, never break."""
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big", signed=True)


def scope_lock_keys(
    user_id: str, api_key_id: int | None, project_id: str | None
) -> list[tuple[int, int]]:
    """The two-key advisory locks of the scopes given, in the order they are taken."""
    keys = [(LOCK_CLASS_ACCOUNT, _hash32(f"u:{user_id}"))]
    if api_key_id is not None:
        keys.append((LOCK_CLASS_KEY, _hash32(f"k:{api_key_id}")))
    if project_id is not None:
        keys.append((LOCK_CLASS_PROJECT, _hash32(f"p:{project_id}")))
    return sorted(set(keys))


def usd(value: float | Decimal) -> Decimal:
    """*value* to the counters' six places, half away from zero (as ``counted_amount``)."""
    return Decimal(value).quantize(_MICRO, rounding=ROUND_HALF_UP)


# -- admission ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """One admission: who, what for, how much it asks to reserve, and which scopes it checks.

    ``api_key_id`` and ``project_id`` are the request's attribution, stored on
    the reservation whatever is checked; ``check_key`` and ``check_project``
    say whether the key's and the project's budgets apply (they are locked and
    read only then). ``requested_usd`` is the estimate asked for; what is
    reserved is decided under the locks and may be less (the headroom clamp).
    """

    reservation_id: str
    instance_id: str
    user_id: str
    api_key_id: int | None
    project_id: str | None
    requested_usd: Decimal
    check_day: str
    operation: str
    check_key: bool = False
    check_project: bool = False

    @property
    def month(self) -> str:
        """The UTC month the check's monthly limits apply to."""
        return self.check_day[:7]

    def digest(self) -> str:
        """What a reuse of :attr:`reservation_id` must match to be the same admission."""
        payload = [
            self.instance_id,
            self.user_id,
            self.api_key_id,
            self.project_id,
            str(self.requested_usd),
            self.check_day,
            self.operation,
        ]
        return hashlib.sha256(json.dumps(payload).encode()).hexdigest()

    def lock_keys(self) -> list[tuple[int, int]]:
        """The scope locks this admission takes."""
        return scope_lock_keys(
            self.user_id,
            self.api_key_id if self.check_key else None,
            self.project_id if self.check_project else None,
        )


@dataclass(frozen=True, slots=True)
class SpendSnapshot:
    """What one admission read under its locks: counters and open reservations, per scope."""

    account_day: Decimal
    account_month: Decimal
    key_day: Decimal
    key_month: Decimal
    project_day: Decimal
    project_month: Decimal
    reserved_account_day: Decimal
    reserved_account_month: Decimal
    reserved_key_day: Decimal
    reserved_key_month: Decimal
    reserved_project_day: Decimal
    reserved_project_month: Decimal


#: ``admitted`` (row inserted), ``existing`` (an earlier attempt of this same
#: admission committed it), ``retired`` (the id was settled, or is held as a
#: liability: it can never admit again), ``refused``, ``erased``, ``conflict``
#: (the id is held for another admission).
AdmissionResult = Literal["admitted", "existing", "retired", "refused", "erased", "conflict"]


@dataclass(slots=True)
class AdmissionOutcome:
    """How one admission transaction ended, and what it reserved."""

    result: AdmissionResult
    reserved_usd: Decimal | None = None
    snapshot: SpendSnapshot | None = None


_LOCK_SCOPES_SQL = """
SELECT txid_current() AS xid,
       (SELECT count(*) FROM (
            SELECT pg_advisory_xact_lock(cls, obj)
            FROM unnest($1::int[], $2::int[]) WITH ORDINALITY AS k(cls, obj, position)
            ORDER BY position
       ) AS locked) AS locks
"""

# Statement B: issued only after the locks were granted, so its snapshot
# follows every earlier holder's commit; counters and reservations in one
# snapshot. Open reservations: an active one counts against the current day
# and month; a quarantined one (the counters refused its costs, so they are in
# none) against the day it ended and that day's month, at the larger of its
# estimate and its known cost. The reservation id's own row and retirement
# receipt come from the same snapshot.
_HELD_FROM = """
    SELECT COALESCE(SUM(held) FILTER (WHERE active OR liability_day = $3), 0) AS day_held,
           COALESCE(SUM(held) FILTER (WHERE active OR left(liability_day, 7) = $4), 0)
               AS month_held
    FROM (
        SELECT state = 'active' AS active, liability_day,
               GREATEST(amount_usd, COALESCE(cost_usd, 0)) AS held
        FROM spend_reservations WHERE {where}
    ) s
"""

_READ_SQL = f"""
SELECT
    r.digest AS existing_digest,
    r.amount_usd AS existing_amount,
    r.state AS existing_state,
    (SELECT batch_id FROM spend_settlement_members WHERE reservation_id = $1) AS retired_by,
    ($7::text IS NOT NULL AND EXISTS (
        SELECT 1 FROM {ERASURE_FENCE_TABLE} WHERE account_digest = $7
    )) AS erased,
    COALESCE(ua.day_cost, 0) AS account_day,
    COALESCE(ua.month_cost, 0) AS account_month,
    COALESCE(ka.day_cost, 0) AS key_day,
    COALESCE(ka.month_cost, 0) AS key_month,
    COALESCE(pa.day_cost, 0) AS project_day,
    COALESCE(pa.month_cost, 0) AS project_month,
    ru.day_held AS reserved_account_day,
    ru.month_held AS reserved_account_month,
    rk.day_held AS reserved_key_day,
    rk.month_held AS reserved_key_month,
    rp.day_held AS reserved_project_day,
    rp.month_held AS reserved_project_month
FROM (SELECT 1) AS one
LEFT JOIN spend_reservations r ON r.reservation_id = $1
CROSS JOIN LATERAL (
    SELECT SUM(cost_usd) FILTER (WHERE day = $3) AS day_cost, SUM(cost_usd) AS month_cost
    FROM user_daily_cost
    WHERE user_id = $2 AND day >= $4 || '-01' AND day <= $4 || '-31'
) ua
CROSS JOIN LATERAL (
    SELECT SUM(cost_usd) FILTER (WHERE day = $3) AS day_cost, SUM(cost_usd) AS month_cost
    FROM api_key_daily_cost
    WHERE $5::bigint IS NOT NULL AND key_id = $5
      AND day >= $4 || '-01' AND day <= $4 || '-31'
) ka
CROSS JOIN LATERAL (
    SELECT SUM(cost_usd) FILTER (WHERE period = 'd:' || $3) AS day_cost,
           SUM(cost_usd) FILTER (WHERE period = 'm:' || $4) AS month_cost
    FROM spend_counters
    WHERE $6::text IS NOT NULL AND scope_id = $6 AND period IN ('d:' || $3, 'm:' || $4)
) pa
CROSS JOIN LATERAL ({_HELD_FROM.format(where="user_id = $2")}) ru
CROSS JOIN LATERAL ({_HELD_FROM.format(where="$5::bigint IS NOT NULL AND api_key_id = $5")}) rk
CROSS JOIN LATERAL ({_HELD_FROM.format(where="$6::text IS NOT NULL AND project_id = $6")}) rp
"""

_INSERT_SQL = """
INSERT INTO spend_reservations
    (reservation_id, instance_id, user_id, api_key_id, project_id, amount_usd,
     check_day, operation, digest)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""


def _timeouts_sql(budget_ms: int) -> str:
    # Integers only, so formatting them in is safe; one round trip with BEGIN.
    # The third bound is for the time *between* statements, which the other
    # two do not cover: the transaction holds its scopes' locks across several
    # round trips, and a process that stalls between two of them (a blocked
    # event loop, a stopped container, a dead network path) would hold them,
    # and every other process's admission for those scopes, until it came
    # back. The server ends such a session instead, which releases the locks.
    budget_ms = max(1, int(budget_ms))
    return (
        "BEGIN ISOLATION LEVEL READ COMMITTED; "
        f"SET LOCAL lock_timeout = '{budget_ms}ms'; "
        f"SET LOCAL statement_timeout = '{budget_ms}ms'; "
        f"SET LOCAL idle_in_transaction_session_timeout = '{budget_ms}ms'"
    )


_CLOSE_SQL = (
    "INSERT INTO spend_instance_closures (instance_id, closed_by, operator, reason) "
    "VALUES ($1, $2, $3, $4) ON CONFLICT (instance_id) DO NOTHING RETURNING TRUE"
)

# A batch receipt goes with its member receipts, in one statement. SKIP LOCKED:
# two processes purging at once take different batches instead of waiting.
_PURGE_DELETE = """
, members AS (
    DELETE FROM spend_settlement_members m USING doomed d WHERE m.batch_id = d.batch_id
    RETURNING 1
), batches AS (
    DELETE FROM spend_settlements s USING doomed d WHERE s.batch_id = d.batch_id
    RETURNING 1
)
SELECT (SELECT count(*) FROM batches) AS batches, (SELECT count(*) FROM members) AS members
"""

_PURGE_OWN_SQL = f"""
WITH doomed AS (
    SELECT batch_id FROM spend_settlements
    WHERE instance_id = $1
      AND settled_at < NOW() - make_interval(secs => $2)
      AND NOT (batch_id = ANY($3::text[]))
    ORDER BY settled_at
    LIMIT $4
    FOR UPDATE SKIP LOCKED
){_PURGE_DELETE}
"""

_PURGE_CLOSED_SQL = f"""
WITH doomed AS (
    SELECT s.batch_id
    FROM spend_instance_closures c
    JOIN spend_settlements s ON s.instance_id = c.instance_id
    WHERE c.closed_at < NOW() - make_interval(secs => $1)
      AND s.settled_at < NOW() - make_interval(secs => $1)
      AND NOT EXISTS (
          SELECT 1 FROM config_instances i
          WHERE i.instance_id = c.instance_id AND i.lifecycle <> 'stopped'
            AND i.heartbeat_at > NOW() - make_interval(secs => $3)
      )
    ORDER BY s.settled_at
    LIMIT $2
    FOR UPDATE OF s SKIP LOCKED
){_PURGE_DELETE}
"""

_PURGE_CLOSURES_SQL = """
DELETE FROM spend_instance_closures c
WHERE c.closed_by = 'shutdown'
  AND c.closed_at < NOW() - make_interval(secs => $1)
  AND NOT EXISTS (SELECT 1 FROM spend_settlements s WHERE s.instance_id = c.instance_id)
  AND NOT EXISTS (SELECT 1 FROM spend_reservations r WHERE r.instance_id = c.instance_id)
"""

# The UTC day a log row was written on is the day its charge is counted on,
# as the day its billing point ran on would have been.
_REQUEST_LOG_SQL = """
SELECT r.reservation_id, l.request_id, l.cost_usd, l.status_code,
       l.metadata->>'spend_charged_usd' AS charged_usd,
       to_char(l.timestamp AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day
FROM unnest($1::text[], $2::text[], $3::timestamptz[]) AS r(reservation_id, user_id, since)
JOIN api_logs l
  ON l.user_id = r.user_id AND l.timestamp >= r.since
 AND l.metadata->>'spend_reservation' = r.reservation_id
ORDER BY l.timestamp, l.request_id
"""

_AUDIT_SQL = """
INSERT INTO spend_reconciliations
    (reservation_id, instance_id, batch_id, amount_usd, operation, admitted_at, outcome,
     cost_usd, evidence, owner_state, operator, reason)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12)
"""


def _count(value: Any) -> int | None:
    """A row count, or None where the planner has no estimate yet."""
    return int(value) if value is not None else None


def owner_state(instance: dict[str, Any] | None, closed_by: str | None) -> str:
    """How a reservation's owner is known: by its closure, or by what the registry says of it.

    ``closed`` when the process recorded its own shutdown, or when an operator
    recorded it gone and the registry does not hear from it. Otherwise the
    registry's word: ``not registered``, ``stale`` (no heartbeat for too long;
    it may be cut off from the database rather than gone), or its lifecycle.
    An instance an operator closed and that is heard from again is alive: its
    lifecycle is what is returned.
    """
    heard = instance is not None and not instance["stale"] and instance["lifecycle"] != "stopped"
    if closed_by == "shutdown" or (closed_by is not None and not heard):
        return "closed"
    if instance is None:
        return "not registered"
    return "stale" if instance["stale"] else str(instance["lifecycle"])


class OwnerHeardFrom(RuntimeError):
    """A reconciliation named an instance the registry still hears from."""

    def __init__(self, instance_id: str, state: str) -> None:
        """Keep the instance and how the registry knows it."""
        super().__init__(f"instance {instance_id} is {state}: it retires its own reservations")
        self.instance_id = instance_id
        self.state = state


class SharedSpendStore:
    """The shared accounting's statements, on an operational store's pools.

    ``request_pool`` serves admissions (the request path, bounded waits);
    ``pool`` the settlement writer and the status view.
    """

    def __init__(self, pool: Any, *, request_pool: Any = None, fence_secret: str | None = None):
        """Keep the pools and the erasure fence's secret (None: no fence)."""
        self.pool = pool
        self.request_pool = request_pool if request_pool is not None else pool
        self.fence_secret = fence_secret

    def fence_digest(self, user_id: str) -> str | None:
        """The erasure-fence digest of *user_id*, or None without a fence secret."""
        return fence_account_digest(user_id, self.fence_secret) if self.fence_secret else None

    async def admit_on(
        self,
        conn: Any,
        request: AdmissionRequest,
        decide: Callable[[SpendSnapshot], Decimal | None],
        *,
        budget_ms: int,
        on_xid: Callable[[int], None],
        pause: Callable[[str], Any] | None = None,
    ) -> AdmissionOutcome:
        """Run one admission transaction on *conn*.

        *decide* applies the budget policy to what was read and returns what to
        reserve, or None to refuse. *on_xid* is handed the transaction's id as
        soon as it is known, so a caller who then loses the answer can learn
        how the transaction ended (``txid_status``). *pause*, for tests only,
        is awaited between the steps (``locked``, ``read``, ``inserted``,
        ``committed``) to pin an interleaving. Raises whatever the database
        raises, after rolling back.

        An id already retired (a settlement receipt names it) or held in a
        terminal state answers ``retired`` and never admits again; an id held
        for a different admission answers ``conflict``.
        """
        await conn.execute(_timeouts_sql(budget_ms))
        committed = False

        async def finish(outcome: AdmissionOutcome) -> AdmissionOutcome:
            nonlocal committed
            await conn.execute("COMMIT")
            committed = True
            return outcome

        try:
            digest_key = self.fence_digest(request.user_id)
            if digest_key is not None:
                await lock_fence_keys_shared(conn, [digest_key])
            keys = request.lock_keys()
            row = await conn.fetchrow(
                _LOCK_SCOPES_SQL, [cls for cls, _ in keys], [obj for _, obj in keys]
            )
            on_xid(int(row["xid"]))
            if pause is not None:
                await pause("locked")
            read = await conn.fetchrow(
                _READ_SQL,
                request.reservation_id,
                request.user_id,
                request.check_day,
                request.month,
                request.api_key_id if request.check_key else None,
                request.project_id if request.check_project else None,
                digest_key,
            )
            if pause is not None:
                await pause("read")
            if read["existing_digest"] is not None:
                if read["existing_digest"] != request.digest():
                    return await finish(AdmissionOutcome("conflict"))
                if read["existing_state"] != "active" or read["retired_by"] is not None:
                    return await finish(AdmissionOutcome("retired"))
                return await finish(
                    AdmissionOutcome("existing", reserved_usd=read["existing_amount"])
                )
            if read["retired_by"] is not None:
                return await finish(AdmissionOutcome("retired"))
            if read["erased"]:
                return await finish(AdmissionOutcome("erased"))
            snapshot = SpendSnapshot(
                **{name: Decimal(read[name]) for name in SpendSnapshot.__slots__}
            )
            amount = decide(snapshot)
            if amount is None:
                return await finish(AdmissionOutcome("refused", snapshot=snapshot))
            await conn.execute(
                _INSERT_SQL,
                request.reservation_id,
                request.instance_id,
                request.user_id,
                request.api_key_id,
                request.project_id,
                amount,
                request.check_day,
                request.operation,
                request.digest(),
            )
            if pause is not None:
                await pause("inserted")
            outcome = await finish(
                AdmissionOutcome("admitted", reserved_usd=amount, snapshot=snapshot)
            )
            if pause is not None:
                await pause("committed")
            return outcome
        finally:
            if not committed and not conn.is_closed():
                # Best effort: a broken connection is discarded by its pool, and
                # the server rolls back a transaction whose COMMIT never came.
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(1.0):
                        await conn.execute("ROLLBACK")

    async def transaction_status(self, xids: Sequence[int]) -> dict[int, str | None]:
        """How each transaction ended: ``committed``, ``aborted``, ``in progress`` or None."""
        if not xids:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT x, txid_status(x) AS status FROM unnest($1::bigint[]) AS x", list(xids)
            )
        return {int(row["x"]): row["status"] for row in rows}

    # -- settlement ----------------------------------------------------------

    async def settle(
        self, batch: SettlementBatch, *, pause: Callable[[str], Any] | None = None
    ) -> SettlementResult:
        """Apply *batch* exactly once; return what became of each member.

        A batch whose own payload is inconsistent (a reservation listed twice,
        or a member carrying a cost attributed to another account, key or
        project than the member's) is refused whole, before anything is
        written: ``invalid``.

        Otherwise one transaction: the batch's id and payload digest are
        recorded first (a second batch with the same id waits for this one and
        then finds it); the members this instance holds *for the same account,
        key and project* are retired; the costs of the retired members and the
        plain increments are added to the counters (fenced as the cost writer
        is); the retirements are recorded, one per reservation. A batch id
        already recorded with the same digest returns the stored result and
        changes nothing; with another digest it is a ``conflict`` and changes
        nothing. A member not retired is reported -- ``duplicate`` (another
        batch retired it), ``mismatch`` (held, but not as this member says),
        ``erased`` or ``missing`` -- and its costs are not added. (``erased``
        is told by the erasure fence, which the gateway always configures; a
        store built without its secret reports a hard-deleted account's
        members as ``missing``.)

        Raises whatever the database raises before or at COMMIT; the caller
        sends the same batch again, which the recorded id makes harmless.
        *pause*, for tests only, is awaited (``before_commit``) with everything
        written and nothing committed.
        """
        problem = batch.inconsistency()
        if problem is not None:
            return SettlementResult(batch.batch_id, "invalid", {"invalid": [problem]})

        async with self.pool.acquire() as conn, conn.transaction():
            result = await self._settle_in(conn, batch)
            if pause is not None and result.outcome == "applied":
                await pause("before_commit")
        return result

    async def _settle_in(self, conn: Any, batch: SettlementBatch) -> SettlementResult:
        """The statements of one settlement, in the caller's transaction on *conn*.

        *batch* is consistent (:meth:`SettlementBatch.inconsistency`). The
        caller commits, or rolls everything back by raising.
        """
        from serving.storage.postgres_operational import (
            COST_INCREMENTS_FENCED_SQL,
            COST_INCREMENTS_SQL,
        )

        await conn.execute(f"SELECT set_config('{WRITER_SETTING}', 'shared', true)")
        inserted = await conn.fetchval(
            "INSERT INTO spend_settlements (batch_id, instance_id, payload_digest) "
            "VALUES ($1, $2, $3) ON CONFLICT (batch_id) DO NOTHING RETURNING TRUE",
            batch.batch_id,
            batch.instance_id,
            batch.digest,
        )
        if not inserted:
            row = await conn.fetchrow(
                "SELECT payload_digest, result FROM spend_settlements WHERE batch_id = $1",
                batch.batch_id,
            )
            if row is None or row["payload_digest"] != batch.digest:
                return SettlementResult(batch.batch_id, "conflict")
            return SettlementResult.from_stored(batch.batch_id, row["result"])

        users = sorted({line.user_id for line in batch.all_costs()} | batch.member_users())
        fence_keys = (
            sorted({self.fence_digest(user) for user in users}) if self.fence_secret else []
        )
        if fence_keys and len(fence_keys) <= FENCE_LOCKS_MAX:
            await lock_fence_keys_shared(conn, fence_keys)
        erased_digests: set[str] = set()
        if fence_keys:
            erased_digests = {
                row["account_digest"]
                for row in await conn.fetch(
                    f"SELECT account_digest FROM {ERASURE_FENCE_TABLE} "
                    "WHERE account_digest = ANY($1::text[])",
                    fence_keys,
                )
            }

        def owned(members: list[Member]) -> tuple[list[Any], ...]:
            return (
                [m.reservation_id for m in members],
                [m.user_id for m in members],
                [m.api_key_id for m in members],
                [m.project_id for m in members],
            )

        retired = {
            row["reservation_id"]
            for row in await conn.fetch(
                "DELETE FROM spend_reservations AS r "
                "USING unnest($1::text[], $2::text[], $3::bigint[], $4::text[]) "
                "  AS m(id, user_id, key_id, project_id) "
                "WHERE r.reservation_id = m.id AND r.instance_id = $5 "
                "  AND r.state = 'active' AND r.user_id = m.user_id "
                "  AND r.api_key_id IS NOT DISTINCT FROM m.key_id "
                "  AND r.project_id IS NOT DISTINCT FROM m.project_id "
                "RETURNING r.reservation_id",
                *owned(list(batch.members)),
                batch.instance_id,
            )
        }
        absent = [m.reservation_id for m in batch.members if m.reservation_id not in retired]
        duplicates: set[str] = set()
        still_held: set[str] = set()
        if absent:
            duplicates = {
                row["reservation_id"]
                for row in await conn.fetch(
                    "SELECT reservation_id FROM spend_settlement_members "
                    "WHERE reservation_id = ANY($1::text[])",
                    absent,
                )
            }
            still_held = {
                row["reservation_id"]
                for row in await conn.fetch(
                    "SELECT reservation_id FROM spend_reservations "
                    "WHERE reservation_id = ANY($1::text[])",
                    absent,
                )
            }

        result = SettlementResult(batch.batch_id, "applied")
        costs: list[CostLine] = list(batch.increments)
        for member in batch.members:
            rid = member.reservation_id
            if rid in retired:
                result.add("settled" if member.costs else "released", rid)
                costs.extend(member.costs)
            elif rid in duplicates:
                result.add("duplicate", rid)
            elif rid in still_held:
                result.add("mismatch", rid)
            elif self.fence_secret and self.fence_digest(member.user_id) in erased_digests:
                result.add("erased", rid)
            else:
                result.add("missing", rid)

        if costs:
            args = (
                [c.user_id for c in costs],
                [c.day for c in costs],
                [c.api_key_id for c in costs],
                [c.cost_usd for c in costs],
                [c.requests for c in costs],
                [c.project_id for c in costs],
            )
            if self.fence_secret:
                await conn.fetchval(
                    COST_INCREMENTS_FENCED_SQL,
                    *args,
                    [self.fence_digest(c.user_id) for c in costs],
                )
            else:
                await conn.fetchval(COST_INCREMENTS_SQL, *args)

        recorded = [
            (rid, outcome)
            for outcome in ("settled", "released")
            for rid in result.members.get(outcome, [])
        ]
        if recorded:
            await conn.execute(
                "INSERT INTO spend_settlement_members (reservation_id, batch_id, outcome) "
                "SELECT id, $3, outcome FROM unnest($1::text[], $2::text[]) AS m(id, outcome)",
                [rid for rid, _ in recorded],
                [outcome for _, outcome in recorded],
                batch.batch_id,
            )
        await conn.execute(
            "UPDATE spend_settlements SET result = $2::jsonb WHERE batch_id = $1",
            batch.batch_id,
            json.dumps(result.members, sort_keys=True),
        )
        return result

    async def quarantine(
        self,
        member: Member,
        instance_id: str,
        *,
        day: str,
        note: str,
    ) -> str:
        """Keep a reservation whose settlement the counters refused, counted, for an operator.

        Marks its row ``quarantined`` with its known cost and the day it ended.
        Repeating it is harmless: a row this instance already quarantined is
        left on its day and answers the same, so a mark whose answer was lost
        can simply be sent again. Returns what was found:

        - ``quarantined``: the row is marked (now, or already);
        - ``retired``: there is no such row and a settlement receipt names the
          reservation, so another batch retired it;
        - ``missing``: neither.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "WITH marked AS ("
                "  UPDATE spend_reservations SET state = 'quarantined', "
                "    liability_day = COALESCE(liability_day, $6), cost_usd = $7, note = $8, "
                "    updated_at = NOW() "
                "  WHERE reservation_id = $1 AND instance_id = $2 "
                "    AND state IN ('active', 'quarantined') "
                "    AND user_id = $3 AND api_key_id IS NOT DISTINCT FROM $4 "
                "    AND project_id IS NOT DISTINCT FROM $5 "
                "  RETURNING 1) "
                "SELECT EXISTS (SELECT 1 FROM marked) AS marked, EXISTS ("
                "  SELECT 1 FROM spend_settlement_members WHERE reservation_id = $1) AS retired",
                member.reservation_id,
                instance_id,
                member.user_id,
                member.api_key_id,
                member.project_id,
                day,
                member.cost_usd(),
                note[:200],
            )
        if row["marked"]:
            return "quarantined"
        return "retired" if row["retired"] else "missing"

    # -- closures and receipt retention --------------------------------------

    async def close_instance(
        self,
        instance_id: str,
        *,
        closed_by: str,
        operator: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Record that *instance_id* will send no settlement again; False if it was recorded.

        ``shutdown``: the process says so itself, with every batch it sent
        answered. ``operator``: someone who checked that the process no longer
        exists says so, by name and with a reason. The first record stands.
        """
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(_CLOSE_SQL, instance_id, closed_by, operator, reason))

    async def owner_states(self, conn: Any = None) -> dict[str, str]:
        """How every instance the registry or a closure names is known (:func:`owner_state`).

        An instance in neither is ``not registered``: the caller's default.
        """
        from serving.config.instances import fetch_instances

        async def read(conn: Any) -> dict[str, str]:
            instances = {i["instance_id"]: i for i in await fetch_instances(conn)}
            closures = {
                row["instance_id"]: row["closed_by"]
                for row in await conn.fetch(
                    "SELECT instance_id, closed_by FROM spend_instance_closures"
                )
            }
            return {
                instance_id: owner_state(instances.get(instance_id), closures.get(instance_id))
                for instance_id in instances.keys() | closures.keys()
            }

        if conn is not None:
            return await read(conn)
        async with self.pool.acquire() as conn:
            return await read(conn)

    async def purge_receipts(
        self,
        instance_id: str,
        unanswered: Sequence[str],
        *,
        retention_seconds: float = RECEIPT_RETENTION_SECONDS,
        limit: int = PURGE_BATCHES,
    ) -> tuple[int, int]:
        """Delete receipts nothing can need again; return (batches, member rows) deleted.

        Two kinds, each at most *limit* batches a call, oldest first, and none
        younger than *retention_seconds*:

        - *instance_id*'s own, except the batches in *unanswered*: those it
          may still send again, which only their receipt makes harmless. The
          caller is that process, so it knows them exactly.
        - Those of a process whose closure is that old, and which the registry
          does not hear from now (a process an operator closed by mistake
          keeps its receipts, and deletes its own as above).

        A receipt of a process that is neither -- it stopped without closing,
        and no operator has said it is gone -- stays, whatever its age.
        """
        from serving.config.instances import STALE_AFTER_SECONDS

        async with self.pool.acquire() as conn:
            own = await conn.fetchrow(
                _PURGE_OWN_SQL, instance_id, retention_seconds, list(unanswered), limit
            )
            closed = await conn.fetchrow(
                _PURGE_CLOSED_SQL, retention_seconds, limit, STALE_AFTER_SECONDS
            )
        return (
            int(own["batches"]) + int(closed["batches"]),
            int(own["members"]) + int(closed["members"]),
        )

    async def purge_closures(self, *, retention_seconds: float = RECEIPT_RETENTION_SECONDS) -> int:
        """Delete the closures of processes that left nothing behind; return how many.

        Only one a process recorded itself (an operator's is part of the audit
        record), that old, and with no receipt and no reservation left under
        its instance id.
        """
        async with self.pool.acquire() as conn:
            status = await conn.execute(_PURGE_CLOSURES_SQL, retention_seconds)
        return int(status.rsplit(" ", 1)[-1])

    # -- reconciling what a process that is gone left --------------------------

    async def reservations_of(
        self, instance_id: str, *, limit: int, exclude: Sequence[str] = ()
    ) -> list[dict[str, Any]]:
        """The active reservations *instance_id* holds, oldest first (at most *limit*).

        Without those in *exclude*: the ones a reconciliation found it cannot
        retire, so that it goes on to the rest.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT reservation_id, user_id, api_key_id, project_id, amount_usd, operation, "
                "created_at, EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_seconds "
                "FROM spend_reservations WHERE instance_id = $1 AND state = 'active' "
                "AND NOT (reservation_id = ANY($3::text[])) "
                "ORDER BY created_at, reservation_id LIMIT $2",
                instance_id,
                limit,
                list(exclude),
            )
        return [dict(row) for row in rows]

    async def instance_known(self, instance_id: str) -> bool:
        """Whether anything names *instance_id*: a reservation, a receipt, a registration, a closure.

        An id nothing names is a mistyped one, or a process that left nothing
        behind; either way there is nothing to reconcile and nothing to close.
        """
        async with self.pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM spend_reservations WHERE instance_id = $1) "
                    "OR EXISTS (SELECT 1 FROM spend_settlements WHERE instance_id = $1) "
                    "OR EXISTS (SELECT 1 FROM config_instances WHERE instance_id = $1) "
                    "OR EXISTS (SELECT 1 FROM spend_instance_closures WHERE instance_id = $1)",
                    instance_id,
                )
            )

    async def held_by(self, instance_id: str) -> dict[str, int]:
        """How many reservations *instance_id* holds, by state."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT state, COUNT(*) AS n FROM spend_reservations "
                "WHERE instance_id = $1 GROUP BY state",
                instance_id,
            )
        return {row["state"]: int(row["n"]) for row in rows}

    async def request_log_rows(
        self, reservations: Sequence[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """The request-log rows written under each of *reservations*, by reservation id.

        A request's log row names the reservation it ran under
        (``metadata.spend_reservation``) and what the billing points had
        recorded under it when the row was written
        (``metadata.spend_charged_usd``); both are set as the row is written,
        by ``CompletionsLogger``. Rows are looked up by the reservation's
        account from its admission on, which the log's own index serves. A
        reservation with no row is absent from the result: its request never
        got as far as being logged, or the row was dropped or not written.
        """
        if not reservations:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                _REQUEST_LOG_SQL,
                [r["reservation_id"] for r in reservations],
                [r["user_id"] for r in reservations],
                [r["created_at"] for r in reservations],
            )
        found: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            found.setdefault(row["reservation_id"], []).append(
                {
                    "request_id": row["request_id"],
                    "day": row["day"],
                    "cost_usd": row["cost_usd"],
                    "charged_usd": row["charged_usd"],
                    "status_code": row["status_code"],
                }
            )
        return found

    async def reconcile(
        self,
        instance_id: str,
        plan: Sequence[Reconciliation],
        *,
        operator: str,
        reason: str,
    ) -> tuple[SettlementResult, str]:
        """Retire, for a process that is gone, the reservations *plan* names; audit each.

        One transaction. The owner must be known as gone *now*
        (:data:`OWNER_GONE`): a process the registry hears from retires its own
        reservations, and this refuses (:class:`OwnerHeardFrom`). Then the plan
        is applied as a settlement made in that process's name
        (:meth:`settle`): its rows are retired, the costs the plan found are
        added to the counters, and each retirement is recorded -- so a
        settlement that process turns out to send after all finds its
        reservations ``duplicate`` and adds nothing. Every reservation retired
        here gets its row in ``spend_reconciliations``. One the batch did not
        retire (already retired, erased, or no longer held as planned) is
        reported in the result and not audited: nothing was done to it.

        Returns the settlement's result and how the owner was known.
        """
        import uuid

        batch = SettlementBatch.make(
            instance_id, [entry.member for entry in plan], [], batch_id=f"op_{uuid.uuid4().hex}"
        )
        problem = batch.inconsistency()
        if problem is not None:
            raise ValueError(f"inconsistent reconciliation plan: {problem}")
        by_id = {entry.reservation_id: entry for entry in plan}
        async with self.pool.acquire() as conn, conn.transaction():
            state = (await self.owner_states(conn)).get(instance_id, "not registered")
            if state not in OWNER_GONE:
                raise OwnerHeardFrom(instance_id, state)
            result = await self._settle_in(conn, batch)
            retired = [
                (by_id[rid], outcome)
                for outcome in ("settled", "released")
                for rid in result.members.get(outcome, [])
            ]
            if retired:
                await conn.executemany(
                    _AUDIT_SQL,
                    [
                        (
                            entry.reservation_id,
                            instance_id,
                            batch.batch_id,
                            entry.amount_usd,
                            entry.operation,
                            entry.admitted_at,
                            outcome,
                            entry.member.cost_usd(),
                            json.dumps(list(entry.evidence), default=str),
                            state,
                            operator,
                            reason,
                        )
                        for entry, outcome in retired
                    ],
                )
        return result, state

    # -- the read-only status view -------------------------------------------

    async def read_mode(self) -> str:
        """The stored mode."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT mode FROM spend_accounting WHERE id") or "local"

    async def open_reservations(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Every reservation not retired, oldest first (at most *limit*); no account names."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT reservation_id, instance_id, amount_usd, cost_usd, state, check_day, "
                "liability_day, operation, note, created_at, "
                "EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_seconds "
                "FROM spend_reservations ORDER BY created_at LIMIT $1",
                limit,
            )
            owners = await self.owner_states(conn)
        return [
            {**dict(row), "owner": owners.get(row["instance_id"], "not registered")} for row in rows
        ]

    async def summary(self, *, exact: bool = True) -> dict[str, Any]:
        """Counts for the status report and the hourly survey, every process's included.

        Open reservations by instance and state, each with how its owner is
        known (:func:`owner_state`); the receipts kept and the oldest one's
        age; closures by kind; what operators reconciled; and the evidence of
        counter writes made outside the shared settlement.

        The receipts are counted, and the oldest found, by reading both
        receipt tables whole: fine for a command an operator runs, not for
        every process every hour. With *exact* false they are the planner's
        estimates (None before the tables were first analyzed) and the oldest
        age is left out.
        """
        async with self.pool.acquire() as conn:
            by_state = await conn.fetch(
                "SELECT instance_id, state, COUNT(*) AS n, SUM(amount_usd) AS amount_usd, "
                "SUM(cost_usd) AS known_cost_usd, "
                "EXTRACT(EPOCH FROM (NOW() - MIN(created_at))) AS oldest_age_seconds "
                "FROM spend_reservations GROUP BY instance_id, state ORDER BY instance_id, state"
            )
            unidentified = await conn.fetchrow(
                "SELECT COUNT(*) AS n, MAX(written_at) AS last FROM spend_unidentified_writes"
            )
            receipts = await conn.fetchrow(
                "SELECT (SELECT COUNT(*) FROM spend_settlements) AS batches, "
                "(SELECT COUNT(*) FROM spend_settlement_members) AS members, "
                "(SELECT EXTRACT(EPOCH FROM (NOW() - MIN(settled_at))) FROM spend_settlements) "
                "AS oldest_age_seconds"
                if exact
                else "SELECT "
                "(SELECT NULLIF(reltuples, -1)::bigint FROM pg_class "
                " WHERE oid = 'spend_settlements'::regclass) AS batches, "
                "(SELECT NULLIF(reltuples, -1)::bigint FROM pg_class "
                " WHERE oid = 'spend_settlement_members'::regclass) AS members, "
                "NULL::float8 AS oldest_age_seconds"
            )
            closures = await conn.fetch(
                "SELECT closed_by, COUNT(*) AS n FROM spend_instance_closures GROUP BY closed_by"
            )
            reconciled = await conn.fetchrow(
                "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS cost_usd, "
                "MAX(reconciled_at) AS last FROM spend_reconciliations"
            )
            owners = await self.owner_states(conn)
        oldest_receipt = receipts["oldest_age_seconds"]
        return {
            "reservations": [
                {
                    **dict(row),
                    "oldest_age_seconds": round(float(row["oldest_age_seconds"]), 1),
                    "owner": owners.get(row["instance_id"], "not registered"),
                }
                for row in by_state
            ],
            "unidentified_writes": int(unidentified["n"] or 0),
            "last_unidentified_write": unidentified["last"],
            "receipts": {
                "batches": _count(receipts["batches"]),
                "members": _count(receipts["members"]),
                "oldest_age_seconds": (
                    round(float(oldest_receipt), 1) if oldest_receipt is not None else None
                ),
                "estimated": not exact,
            },
            "closures": {row["closed_by"]: int(row["n"]) for row in closures},
            "reconciled": {
                "reservations": int(reconciled["n"]),
                "cost_usd": reconciled["cost_usd"],
                "last": reconciled["last"],
            },
        }


# -- settlement payloads -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostLine:
    """One addition to the counters: a cost, to the counters' six places, and its attribution."""

    user_id: str
    day: str
    api_key_id: int | None
    cost_usd: Decimal
    requests: int = 1
    project_id: str | None = None

    def canonical(self) -> list[Any]:
        """The line as the payload digest sees it."""
        return [
            self.user_id,
            self.day,
            self.api_key_id,
            str(self.cost_usd),
            self.requests,
            self.project_id,
        ]


MemberKind = Literal["settle", "release"]


@dataclass(frozen=True, slots=True)
class Member:
    """One reservation a batch retires: ``settle`` (with its costs, if any) or ``release``.

    ``settle`` is a request that ended, with whatever its billing points
    recorded; ``release`` an admission that never became a request. ``user_id``,
    ``api_key_id`` and ``project_id`` are the reservation's own attribution;
    settlement retires the row only when they match it, and every cost the
    member carries must be attributed the same way.
    """

    reservation_id: str
    user_id: str
    kind: MemberKind
    api_key_id: int | None = None
    project_id: str | None = None
    costs: tuple[CostLine, ...] = ()

    def cost_usd(self) -> Decimal:
        """The member's known cost."""
        return sum((c.cost_usd for c in self.costs), Decimal(0))

    def canonical(self) -> list[Any]:
        """The member as the payload digest sees it."""
        return [
            self.reservation_id,
            self.user_id,
            self.api_key_id,
            self.project_id,
            self.kind,
            [c.canonical() for c in self.costs],
        ]


@dataclass(frozen=True, slots=True)
class SettlementBatch:
    """A batch: its id, its frozen members and plain increments, and their digest."""

    batch_id: str
    instance_id: str
    members: tuple[Member, ...]
    increments: tuple[CostLine, ...]
    digest: str = field(default="")

    @classmethod
    def make(
        cls,
        instance_id: str,
        members: Sequence[Member],
        increments: Sequence[CostLine],
        *,
        batch_id: str | None = None,
    ) -> SettlementBatch:
        """A new batch with a fresh id (or *batch_id*) and its digest computed."""
        import uuid

        members = tuple(sorted(members, key=lambda m: m.reservation_id))
        increments = tuple(increments)
        payload = {
            "instance": instance_id,
            "members": [m.canonical() for m in members],
            "increments": [line.canonical() for line in increments],
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return cls(batch_id or uuid.uuid4().hex, instance_id, members, increments, digest)

    def inconsistency(self) -> str | None:
        """Why the batch's own payload cannot be applied, or None."""
        ids = [m.reservation_id for m in self.members]
        if len(ids) != len(set(ids)):
            return "a reservation is listed more than once"
        for member in self.members:
            if member.kind == "release" and member.costs:
                return f"release {member.reservation_id} carries costs"
            for line in member.costs:
                if (line.user_id, line.api_key_id, line.project_id) != (
                    member.user_id,
                    member.api_key_id,
                    member.project_id,
                ):
                    return f"a cost of {member.reservation_id} is attributed elsewhere"
        return None

    def all_costs(self) -> list[CostLine]:
        """Every cost line the batch carries, members' included."""
        return [*self.increments, *(c for m in self.members for c in m.costs)]

    def member_users(self) -> set[str]:
        """The accounts of the batch's members."""
        return {m.user_id for m in self.members}


Outcome = Literal["applied", "replayed", "conflict", "invalid"]


@dataclass(slots=True)
class SettlementResult:
    """What one settlement did: ``applied`` now, ``replayed`` (applied before), or refused."""

    batch_id: str
    outcome: Outcome
    members: dict[str, list[str]] = field(default_factory=dict)

    def add(self, what: str, reservation_id: str) -> None:
        """Record what became of *reservation_id*."""
        self.members.setdefault(what, []).append(reservation_id)

    @classmethod
    def from_stored(cls, batch_id: str, stored: Any) -> SettlementResult:
        """The result an earlier commit of the same batch stored."""
        members = json.loads(stored) if isinstance(stored, str) else dict(stored or {})
        return cls(batch_id, "replayed", {k: list(v) for k, v in members.items()})

    def outcome_of(self, reservation_id: str) -> str | None:
        """What became of *reservation_id* in this batch, if it was a member."""
        for what, ids in self.members.items():
            if reservation_id in ids:
                return what
        return None


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What an operator's reconciliation does with one reservation, and on what evidence.

    ``member`` is how the reservation is retired: with the charge its
    request-log rows record (``evidence``, one entry a row), or with none when
    no row records one -- then nothing is charged and its whole estimate is
    given back.
    """

    reservation_id: str
    amount_usd: Decimal
    operation: str
    admitted_at: Any
    member: Member
    evidence: tuple[dict[str, Any], ...] = ()


def fence_advisory_key(user_id: str, secret: str) -> int:
    """The 64-bit fence lock key of *user_id* (for tests that hold it)."""
    return fence_account_advisory_key(fence_account_digest(user_id, secret))
