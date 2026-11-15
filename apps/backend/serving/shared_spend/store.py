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
Nothing here deletes either: they are what makes a repeated batch harmless and
a retired reservation id unusable, and nothing bounds how late a repeat can
come. Nothing here retires a reservation except its owner's settlement.
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
    budget_ms = max(1, int(budget_ms))
    return (
        "BEGIN ISOLATION LEVEL READ COMMITTED; "
        f"SET LOCAL lock_timeout = '{budget_ms}ms'; "
        f"SET LOCAL statement_timeout = '{budget_ms}ms'"
    )


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
        from serving.storage.postgres_operational import (
            COST_INCREMENTS_FENCED_SQL,
            COST_INCREMENTS_SQL,
        )

        problem = batch.inconsistency()
        if problem is not None:
            return SettlementResult(batch.batch_id, "invalid", {"invalid": [problem]})

        async with self.pool.acquire() as conn, conn.transaction():
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
            if pause is not None:
                await pause("before_commit")
        return result

    async def quarantine(
        self,
        member: Member,
        instance_id: str,
        *,
        day: str,
        note: str,
    ) -> bool:
        """Keep a reservation whose settlement the counters refused, counted, for an operator."""
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE spend_reservations SET state = 'quarantined', liability_day = $6, "
                "cost_usd = $7, note = $8, updated_at = NOW() "
                "WHERE reservation_id = $1 AND instance_id = $2 AND state = 'active' "
                "AND user_id = $3 AND api_key_id IS NOT DISTINCT FROM $4 "
                "AND project_id IS NOT DISTINCT FROM $5",
                member.reservation_id,
                instance_id,
                member.user_id,
                member.api_key_id,
                member.project_id,
                day,
                member.cost_usd(),
                note[:200],
            )
        return status.endswith(" 1")

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
        return [dict(row) for row in rows]

    async def summary(self) -> dict[str, Any]:
        """Counts for the status report: open reservations by instance and state, and evidence."""
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
        return {
            "reservations": [
                {**dict(row), "oldest_age_seconds": round(float(row["oldest_age_seconds"]), 1)}
                for row in by_state
            ],
            "unidentified_writes": int(unidentified["n"] or 0),
            "last_unidentified_write": unidentified["last"],
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


def fence_advisory_key(user_id: str, secret: str) -> int:
    """The 64-bit fence lock key of *user_id* (for tests that hold it)."""
    return fence_account_advisory_key(fence_account_digest(user_id, secret))
