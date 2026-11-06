"""The erasure fence: hard-deleted accounts, kept out of ``api_logs`` for good.

A durable, PostgreSQL-level fence every ``api_logs`` writer checks under the
lock the hard delete takes when it establishes the account's tombstone, and the
fingerprint that pins the secret the fence's digests are derived with. The
tables themselves (``erasure_fence``, ``erasure_fence_metadata``) come from the
schema migrations.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    import asyncpg


# ---------------------------------------------------------------------------
# Erasure fence (issue #1421)
#
# A fire-and-forget log write can be queued *before* the hard-delete runs, or
# can BEGIN its INSERT before the purge and COMMIT after it. A second DELETE
# after the op_store wipe shrinks the window but does not close it — so we need
# a durable, Postgres-level fence that every api_logs writer must check under
# the same lock the hard-delete takes when establishing the tombstone.
#
# The fence table stores a non-reversible (HMAC-SHA256) digest of each erased
# account id. Storing a digest rather than the raw id means the fence does not
# reintroduce the raw account identity: given only
# ``erasure_fence.account_digest``, you cannot recover the original user_id
# without the secret key. The digest is computed with a dedicated, stable
# erasure-fence secret so that routine API_KEY_SECRET rotation does not
# invalidate existing fences (see :data:`ERASURE_FENCE_SETTING`).
#
# Concurrency protocol (per account):
#
#   LOG WRITE (one api_logs INSERT) — uses a SHARED lock so concurrent
#   writers for the same account do not serialize behind one another:
#       1. Compute fence_key = HMAC(secret, account) for the user_id and the
#          credential_owner_id the row carries (one key when they are equal).
#       2. BEGIN.
#       3. One statement takes pg_advisory_xact_lock_shared(<fence_key's
#          64-bit key>) on every key, in sorted order. Concurrent shared-lock
#          holders coexist; an exclusive hard-delete blocks until they all
#          COMMIT.
#       4. The next statement reads erasure_fence for those keys and, only if
#          none of them has a row, inserts the api_logs row (it reports which).
#       5. COMMIT. A row that names no account takes no lock and checks no
#          fence: it is a single INSERT, in no transaction of its own.
#
#   The fence is read in the statement *after* the one that took the locks,
#   never in the same one: under READ COMMITTED a statement's snapshot is
#   taken when it starts, so a statement that waited for a hard-delete's
#   exclusive lock would read the fence as it was before that hard-delete
#   committed, miss its tombstone, and write the row it purged. A statement
#   started after the lock was granted sees the tombstone: a transaction is
#   visible as committed before its locks are released. Reading the fence
#   and inserting in that one statement is the same as reading it in a
#   statement of its own first, as the protocol once did: no tombstone for
#   these keys can commit in between, since that takes the exclusive lock
#   this transaction holds shared until it commits.
#
#   HARD DELETE — uses an EXCLUSIVE lock so it waits for all in-flight
#   writers and prevents new ones from crossing the fence:
#       1. BEGIN.
#       2. SELECT pg_advisory_xact_lock(hashtext(fence_key)).
#          This blocks until every in-flight shared-lock holder COMMITs.
#       3. INSERT INTO erasure_fence (account_digest) VALUES (fence_key)
#          ON CONFLICT (account_digest) DO NOTHING.
#       4. DELETE FROM api_logs WHERE user_id = $1
#          OR metadata->>'credential_owner_id' = $1.
#       5. COMMIT.
#
# Ordering proof:
#   - Shared locks coexist with shared locks; exclusive locks wait for all
#     shared holders and are waited on by subsequent shared holders.
#   - If writers hold shared locks first, the hard-delete waits for all of
#     them, then purges their rows. Any writer that started before the
#     hard-delete's exclusive lock either committed (and is purged) or is
#     blocked on the fence row it can now see.
#   - If the hard-delete holds the exclusive lock first, subsequent writers
#     wait for it to COMMIT, then see the fence row and abort.
#   - The hard-delete's INSERT and DELETE run in the same transaction, so
#     once it COMMITs, no identifying row can exist and no new one can be
#     inserted.
#
# Why advisory locks instead of SELECT ... FOR UPDATE:
#   ``SELECT ... FOR UPDATE`` only locks *existing* rows. When the fence row
#   does not yet exist, the SELECT returns empty and locks nothing — the
#   hard-delete could INSERT the fence and DELETE the rows while the log
#   write is paused between the check and the INSERT. Advisory locks
#   serialize on the key itself, regardless of whether the row exists.
#
# Why shared locks for writers:
#   An exclusive lock per writer would serialize all concurrent requests for
#   the same account — a severe hot-path regression. Shared locks let
#   ordinary writers proceed in parallel while still guaranteeing the
#   hard-delete waits for all of them.
#
# Why a trigger alone is insufficient: a BEFORE INSERT trigger cannot see the
# ``erasure_fence`` row being established by a concurrent uncommitted
# hard-delete transaction (READ COMMITTED visibility). The trigger would have
# to issue its own ``pg_advisory_xact_lock`` — which is exactly what the
# explicit protocol above does, and what a trigger would do implicitly anyway.
# We choose the explicit protocol because:
#   (a) it keeps the serialization visible in application code, not hidden in
#       trigger logic;
#   (b) it lets the log write ABORT cleanly (no "trigger raised exception"
#       error path that the fire-and-forget caller would have to swallow); and
#   (c) the same helper (:func:`insert_api_logs_row`) is used by both
#       PostgresLogStore.log_request and DatabaseLogger.log_request, so the
#       two writers cannot drift.
# ---------------------------------------------------------------------------

#: Name of the erasure fence table.
ERASURE_FENCE_TABLE = "erasure_fence"

#: Column name of the digest primary key.
ERASURE_FENCE_DIGEST_COLUMN = "account_digest"


#: Setting key for the dedicated, stable erasure-fence derivation secret.
#: This is separate from API_KEY_SECRET so that routine API-key rotation
#: does not invalidate existing fences (which would allow a previously
#: erased account's logs to be written again — a privacy regression).
#: If unset, the gateway falls back to API_KEY_SECRET and logs a startup
#: warning; however, hard-delete will FAIL_CLOSED if no fence secret is
#: available at erasure time (see :func:`establish_erasure_fence`).
ERASURE_FENCE_SETTING = "ERASURE_FENCE_SECRET"


def fence_account_digest(user_id: str, secret: str) -> str:
    """Return the HMAC-SHA256 digest used as the fence key for *user_id*.

    Deterministic and non-reversible: given the digest and no secret you
    cannot recover the original account id.

    Args:
        user_id: the account identifier being erased.
        secret: a dedicated, stable erasure-fence secret (preferably
            :data`ERASURE_FENCE_SETTING`). Must be stable for the lifetime
            of any fence row — changing it fails startup fingerprint
            validation rather than safely rotating the existing namespace. Do
            NOT use a routinely-rotatable secret like
            API_KEY_SECRET unless you treat it as a stable erasure-fence secret.
            Once the fallback namespace is pinned, changing API_KEY_SECRET
            fails startup fingerprint validation; it does not safely rotate
            the existing fence namespace.
    """
    import hmac as _hmac

    mac = _hmac.new(secret.encode(), user_id.encode(), "sha256")
    return mac.hexdigest()


def fence_account_advisory_key(digest: str) -> int:
    """Derive a signed 64-bit Postgres advisory-lock key from a fence digest.

    Postgres advisory-lock functions take a single ``bigint`` key. We take
    the first 64 bits of the HMAC-SHA256 digest and interpret them as a
    signed big-endian int64. This preserves the full 256-bit collision
    resistance of the HMAC for all practical table sizes while giving
    Postgres a native integer key (no ``hashtext`` 32-bit reduction).
    """
    raw = bytes.fromhex(digest[:16])
    key = int.from_bytes(raw, byteorder="big", signed=True)
    return key

    # The PRIMARY KEY already creates the unique index; this is just a
    # comment-friendly anchor for future schema work. Kept separate from the
    # table DDL so the PK's implicit index is never accidentally dropped.


async def check_erasure_fence(
    conn: asyncpg.Connection,
    *,
    fence_keys: list[str],
) -> bool:
    """Return True if any *fence_key* currently has an active erasure tombstone.

    Takes a transaction-level SHARED advisory lock on each key so the caller
    can rely on the serialization described in the module docstring: a
    concurrent hard-delete establishing the same fence key holds an exclusive
    lock and blocks until all shared holders commit. If the hard-delete has
    already committed, its fence row is visible and we return True.

    Call this inside a transaction that subsequently either:
    - aborts (when this returns True — the row must not be written), or
    - inserts the api_logs row and commits (when this returns False).

    Args:
        conn: an asyncpg connection already inside a transaction.
        fence_keys: one or more HMAC digests (as returned by
            :func:`fence_account_digest`) to check. A log row may identify
            its account via ``user_id`` *or* ``metadata->>'credential_owner_id'``,
            so both digests are passed together.
    """
    if not fence_keys:
        return False
    # Sort keys to ensure consistent lock acquisition order and prevent
    # deadlocks between concurrent log writes checking overlapping key sets.
    fence_keys = sorted(set(fence_keys))
    # Take a transaction-level SHARED advisory lock on each key. Shared
    # locks coexist with other shared locks (concurrent writers for the
    # same account proceed in parallel) but block behind an exclusive
    # hard-delete lock and are waited on by subsequent exclusive lockers.
    await lock_fence_keys_shared(conn, fence_keys)
    row = await conn.fetchrow(
        f"""
        SELECT 1
        FROM {ERASURE_FENCE_TABLE}
        WHERE account_digest = ANY($1::text[])
        """,
        fence_keys,
    )
    return row is not None


async def protect_api_logs_insert(
    conn: asyncpg.Connection,
    *,
    fence_secret: str,
    user_id: str | None,
    credential_owner_id: str | None,
) -> bool:
    """Check the erasure fence before inserting an api_logs row.

    Determines the account identifiers this row could carry, takes a shared
    advisory lock on each, and checks for an active erasure tombstone. Returns
    True if the insert should be suppressed (fenced), False if it's safe to
    proceed.

    Call this inside the same transaction as the INSERT.

    Args:
        conn: an asyncpg connection already inside a transaction.
        fence_secret: the server's erasure-fence derivation secret.
        user_id: the ``user_id`` that will be stored in the row (or None).
        credential_owner_id: the ``metadata->>'credential_owner_id'`` that
            will be stored in the row (or None).
    """
    fence_keys = api_logs_fence_keys(
        fence_secret=fence_secret, user_id=user_id, credential_owner_id=credential_owner_id
    )
    return await check_erasure_fence(conn, fence_keys=fence_keys)


def api_logs_fence_keys(
    *,
    fence_secret: str | None,
    user_id: str | None,
    credential_owner_id: str | None,
) -> list[str]:
    """Return the fence digests an ``api_logs`` row is checked against, sorted.

    A row may belong to both the authenticated user and the credential owner.
    Every distinct identity participates in the erasure fence; fencing either
    one suppresses the write. Empty when the row names no account, or when no
    fence secret is configured.

    The order is the order the shared locks are taken in, the same for every
    writer, so writers checking overlapping keys cannot deadlock.
    """
    if not fence_secret or not (user_id or credential_owner_id):
        return []
    fence_keys = set()
    if user_id:
        fence_keys.add(fence_account_digest(user_id, fence_secret))
    if credential_owner_id and credential_owner_id != user_id:
        fence_keys.add(fence_account_digest(credential_owner_id, fence_secret))
    return sorted(fence_keys)


# The shared locks of one row in a single statement. A function scan WITH
# ORDINALITY returns its rows in ordinality order -- the planner relies on that
# to satisfy the ORDER BY without a sort -- and the select list is evaluated
# row by row as they come, so the locks are taken in the order of the array.
_LOCK_SHARED_SQL = """
SELECT pg_advisory_xact_lock_shared(key)
FROM unnest($1::bigint[]) WITH ORDINALITY AS keys(key, position)
ORDER BY position
"""


async def lock_fence_keys_shared(conn: asyncpg.Connection, fence_keys: list[str]) -> None:
    """Take the transaction-level SHARED advisory lock on each key, in order, in one statement.

    Args:
        conn: an asyncpg connection already inside a transaction.
        fence_keys: HMAC digests, in the order to lock them (sorted, for
            every writer alike).
    """
    await conn.execute(_LOCK_SHARED_SQL, [fence_account_advisory_key(key) for key in fence_keys])


# An api_logs INSERT and its fence check in one statement: the row is written
# only when no tombstone covers the keys (the last parameter), and the statement
# answers whether one did. ``fence`` is computed once, under the statement's
# snapshot, which is also the one the INSERT runs under.
_FENCED_INSERT_SQL = """
WITH fence AS (
    SELECT EXISTS (
        SELECT 1 FROM {table} WHERE account_digest = ANY(${keys}::text[])
    ) AS hit
),
inserted AS (
    INSERT INTO api_logs ({columns})
    SELECT {values}
    FROM fence
    WHERE NOT fence.hit
    ON CONFLICT (request_id) DO NOTHING
)
SELECT hit FROM fence
"""

_PLAIN_INSERT_SQL = """
INSERT INTO api_logs ({columns})
VALUES ({values})
ON CONFLICT (request_id) DO NOTHING
"""


@functools.cache
def _api_logs_insert_sql(columns: str, values: str, arg_count: int) -> tuple[str, str]:
    """The plain and the fenced statement for one writer's column and value lists."""
    plain = _PLAIN_INSERT_SQL.format(columns=columns, values=values)
    fenced = _FENCED_INSERT_SQL.format(
        table=ERASURE_FENCE_TABLE, keys=arg_count + 1, columns=columns, values=values
    )
    return plain, fenced


async def insert_api_logs_row(
    conn: asyncpg.Connection,
    *,
    columns: str,
    values: str,
    args: Sequence[Any],
    fence_keys: Sequence[str],
) -> bool:
    """Insert one ``api_logs`` row, unless the erasure fence covers an account it names.

    The write protocol in the comment at the top of this module, in as few
    round trips as it allows: BEGIN, the shared locks in one statement, the
    fence check and the INSERT in the next, COMMIT. A row that names no account
    (*fence_keys* empty) is one INSERT, in no transaction of its own. A
    ``request_id`` already logged is left as it is (``ON CONFLICT DO NOTHING``).

    Args:
        conn: an asyncpg connection. Inside a transaction already, the locks
            are held until that one ends.
        columns: the ``api_logs`` columns written, comma separated.
        values: their values, as SQL over the parameters ``$1`` ... ``$n``.
        args: the *n* parameters.
        fence_keys: from :func:`api_logs_fence_keys`.

    Returns:
        True when a tombstone suppressed the row.
    """
    plain, fenced = _api_logs_insert_sql(columns, values, len(args))
    if not fence_keys:
        await conn.execute(plain, *args)
        return False
    async with conn.transaction():
        await lock_fence_keys_shared(conn, list(fence_keys))
        return bool(await conn.fetchval(fenced, *args, list(fence_keys)))


class ErasureFenceUnavailable(RuntimeError):
    """The erasure fence could not be established because no fence secret is available.

    This is distinct from a transient lock timeout: the connection is
    healthy, but the deployment has not configured a stable erasure-fence
    secret and the fallback is also missing. Callers should treat this as
    a hard failure and abort the hard-delete rather than silently proceeding
    without the fence.
    """


def _non_blank_secret(value: str | None) -> str | None:
    """Return a normalized secret, or ``None`` when it is blank."""
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def resolve_fence_secret(secret: str | None) -> str:
    """Resolve the erasure-fence derivation secret.

    Prefers the dedicated ``ERASURE_FENCE_SECRET`` setting (or the
    *secret* argument if explicitly passed), falls back to
    ``API_KEY_SECRET`` if set, and raises :class:`ErasureFenceUnavailable`
    if neither is available.

    The canonical fence-secret source is the ``erasure_fence_secret``
    setting in ``serving.config.settings.Settings``. This is separate
    from ``API_KEY_SECRET`` so that routine API-key rotation does not
    invalidate existing fences. If the erasure-fence secret changes, restore
    the original secret instead; existing tombstones must remain intact
    because they are the durable deletion record.
    """
    # 1. Explicitly passed secret (for tests/diagnostics)
    if secret and secret.strip():
        return secret.strip()
    # 2. Dedicated setting (preferred for production)
    from serving.config.settings import get_settings

    settings = get_settings()
    dedicated_secret = _non_blank_secret(settings.erasure_fence_secret)
    if dedicated_secret is not None:
        return dedicated_secret
    # 3. Fallback to API_KEY_SECRET (with warning)
    import logging as _logging

    _log = _logging.getLogger(__name__)
    fallback_secret = _non_blank_secret(settings.api_key_secret)
    if fallback_secret is not None:
        _log.warning(
            "Erasure fence: ERASURE_FENCE_SECRET is empty, so the fence derives "
            "its digests from API_KEY_SECRET. The first start with a database "
            "pins that choice: from then on keep API_KEY_SECRET unchanged and "
            "ERASURE_FENCE_SECRET empty, since startup fingerprint validation "
            "refuses any other secret. Only a deployment that has never started "
            "can choose a dedicated ERASURE_FENCE_SECRET. If validation fails, "
            "restore the pinned value instead of deleting fence data.",
        )
        return fallback_secret
    raise ErasureFenceUnavailable(
        "No erasure-fence secret is configured. Set erasure_fence_secret "
        "(or api_key_secret as a fallback) to enable the hard-delete "
        "erasure fence. Without it, the hard-delete endpoint cannot "
        "guarantee that fire-and-forget log writes will not recreate "
        "identifying rows."
    )


async def establish_erasure_fence(
    conn: asyncpg.Connection,
    *,
    fence_key: str,
) -> None:
    """Create the erasure tombstone for one account.

    Uses ``ON CONFLICT DO NOTHING`` so concurrent hard-delete calls for the
    same account are idempotent (no unique-violation error). A
    transaction-level EXCLUSIVE advisory lock on the key serializes against
    any log write holding a shared lock on the same key: the hard-delete
    blocks until all shared holders commit, then establishes the fence and
    purges.

    Args:
        conn: an asyncpg connection already inside a transaction.
        fence_key: the HMAC digest of the account being erased.
    """
    # Take the EXCLUSIVE advisory lock BEFORE inserting the fence row.
    # This blocks until every in-flight shared-lock holder commits, then
    # prevents new shared lockers from crossing the fence.
    _adv_key = fence_account_advisory_key(fence_key)
    await conn.execute("SELECT pg_advisory_xact_lock($1)", _adv_key)
    await conn.execute(
        f"""
        INSERT INTO {ERASURE_FENCE_TABLE} (account_digest)
        VALUES ($1)
        ON CONFLICT (account_digest) DO NOTHING
        """,
        fence_key,
    )


# ---------------------------------------------------------------------------
# Secret fingerprint pinning
# ---------------------------------------------------------------------------

_FINGERPRINT_CONTEXT = "hybridinference:erasure-fence:secret-fingerprint:v1"
_TABLE = "erasure_fence_metadata"
_KEY_COL = "config_key"
_VALUE_COL = "config_value"
_FINGERPRINT_KEY = "secret_fingerprint"
# A separate advisory-lock namespace serializes fingerprint initialization and
# the first tombstone established by a hard delete. The two-int form cannot
# collide with the one-bigint account locks used by the erasure protocol.
_FINGERPRINT_LOCK_CLASS = 0x45524153  # ASCII "ERAS"
_FINGERPRINT_LOCK_OBJECT = 1


def fingerprint_secret(secret: str) -> str:
    """Return a stable, domain-separated fingerprint of the fence secret."""
    import hashlib as _hashlib
    import hmac as _hmac

    return _hmac.new(_FINGERPRINT_CONTEXT.encode(), secret.encode(), _hashlib.sha256).hexdigest()


async def get_pinned_fingerprint(conn: asyncpg.Connection) -> str | None:
    """Return the pinned secret fingerprint, or None if not yet set."""
    return await conn.fetchval(
        f"""
        SELECT {_VALUE_COL}
        FROM {_TABLE}
        WHERE {_KEY_COL} = $1
        """,
        _FINGERPRINT_KEY,
    )


async def try_insert_fingerprint(conn: asyncpg.Connection, *, fingerprint: str) -> bool:
    """Attempt to insert the fingerprint.

    Return ``True`` if inserted, or ``False`` if a fingerprint already
    existed and ``ON CONFLICT DO NOTHING`` skipped the insert.
    """
    result = await conn.execute(
        f"""
        INSERT INTO {_TABLE} ({_KEY_COL}, {_VALUE_COL})
        VALUES ($1, $2)
        ON CONFLICT ({_KEY_COL}) DO NOTHING
        """,
        _FINGERPRINT_KEY,
        fingerprint,
    )
    # asyncpg returns 'INSERT 0 1' for a successful insert, 'INSERT 0 0' if
    # ON CONFLICT DO NOTHING skipped.
    return result.endswith("1")


async def _validate_or_init_fingerprint_in_transaction(
    conn: asyncpg.Connection,
    *,
    secret: str,
) -> None:
    current = fingerprint_secret(secret)

    # Serialize the empty-fence transition with the hard-delete path. Without
    # this lock, a startup could repin metadata while a first tombstone is
    # being committed under a different secret.
    await conn.execute(
        "SELECT pg_advisory_xact_lock($1, $2)",
        _FINGERPRINT_LOCK_CLASS,
        _FINGERPRINT_LOCK_OBJECT,
    )
    stored = await get_pinned_fingerprint(conn)
    # Pin the namespace before the process starts serving. This must happen
    # even when the fence table is empty: otherwise two workers can validate
    # different secrets before the first deletion, then use incompatible
    # digest namespaces after that deletion commits.
    if stored is None:
        inserted = await try_insert_fingerprint(conn, fingerprint=current)
        if inserted:
            return
        stored = await get_pinned_fingerprint(conn)
    if stored != current:
        raise ErasureFenceUnavailable(
            "The configured erasure-fence secret does not match the "
            "pinned fingerprint. The secret was likely rotated after "
            "fence rows were created. Restore the original "
            "ERASURE_FENCE_SECRET before starting this process; do not "
            "delete erasure_fence tombstones or fingerprint metadata."
        )


async def validate_or_init_fingerprint(
    conn: asyncpg.Connection,
    *,
    secret: str,
) -> None:
    """Validate the configured secret while holding the fingerprint lock.

    The first successful initializer pins the derivation namespace before the
    process can serve requests. Every subsequent initializer must match it,
    including when no erasure tombstone exists yet. This prevents workers
    starting with different secrets from passing startup and later using
    incompatible fence namespaces.

    The validation sequence owns a transaction when the caller has not
    already opened one. Callers such as hard-delete that already hold the
    transaction keep that outer transaction, so the advisory lock remains
    held through the fence write and purge.

    Raises :class:`ErasureFenceUnavailable` on mismatch.
    """
    if conn.is_in_transaction():
        await _validate_or_init_fingerprint_in_transaction(conn, secret=secret)
        return
    async with conn.transaction():
        await _validate_or_init_fingerprint_in_transaction(conn, secret=secret)
