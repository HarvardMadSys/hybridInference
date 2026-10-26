"""Admission steps the batch entry points share, in the order they must run.

Every create route -- inline and file ``/v1/batches``, and the Anthropic
``/v1/messages/batches`` -- goes through the same sequence:

1. **Identity**: the full key and account checks, without the daily quota
   (``verify_batch_management_identity``). A revoked key or a suspended account
   is refused here, before anything else.
2. **Body**: read under ``BATCH_MAX_BODY_MB``.
3. **Idempotency** (optional ``Idempotency-Key`` header, our extension): a key
   the same owner already used on the same API surface returns that batch when
   the request is the same (its digest: the normalized body, endpoint,
   metadata, protocol version), answers 409 when it differs and 410 when that
   batch was deleted. A replay is decided **before** the role, quota and caps
   below, so an owner whose quota ran out or whose role was lowered can still
   learn what their earlier request created; it never changes the batch, and
   in particular never the key it runs under. The key is never part of the
   digest, so the owner's other keys replay it too.
4. **Role, daily quota, feature switch** -- only for a request that creates.
5. **Validation** of the requests (:mod:`serving.batch.targets`).
6. **The create transaction**, under the owner's admission lock: the
   idempotency record checked again (a concurrent create with the same key
   returns the first one's batch), the active-batch and queued-request caps,
   and the insert, together (:meth:`BatchStore.create_batch`).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from serving.storage.batch_store import (
    BatchLimitExceeded,
    BatchOwnerUnavailable,
    IdempotencyConflict,
    IdempotencyTombstoned,
)
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.batch.config import BatchConfig

logger = get_logger(__name__)

IDEMPOTENCY_HEADER = "idempotency-key"
_MAX_IDEMPOTENCY_KEY = 255


def idempotency_key(request: Request) -> str | None:
    """The request's ``Idempotency-Key``, or None.

    Raises:
        HTTPException: 400 for an empty, over-long or non-printable key.
    """
    raw = request.headers.get(IDEMPOTENCY_HEADER)
    if raw is None:
        return None
    key = raw.strip()
    if not key or len(key) > _MAX_IDEMPOTENCY_KEY or not key.isprintable():
        raise HTTPException(
            400,
            f"Idempotency-Key must be 1 to {_MAX_IDEMPOTENCY_KEY} printable characters",
        )
    return key


def request_digest(surface: str, **parts: Any) -> str:
    """A digest of what a create request asks for (never of who asks)."""
    canonical = json.dumps(
        {"surface": surface, **parts}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def find_replay(
    store: Any, *, owner: str, surface: str, key: str | None, digest: str
) -> dict[str, Any] | None:
    """The batch an idempotent replay returns, or None to go on creating.

    Raises:
        HTTPException: 409 when the key was used for a different request, 410
            when its batch was deleted.
    """
    if key is None:
        return None
    record = await store.find_idempotent(owner_id=owner, api_surface=surface, key=key)
    if record is None:
        return None
    if record["request_digest"] != digest:
        raise _conflict()
    if record["deleted"]:
        raise _gone()
    row = await store.get_batch(record["batch_id"], user_id=owner)
    if row is None:
        raise _gone()
    return row


def _conflict() -> HTTPException:
    return HTTPException(
        409,
        "This Idempotency-Key was already used for a different request; use a new key",
    )


def _gone() -> HTTPException:
    return HTTPException(
        410,
        "The batch this Idempotency-Key created has been deleted; use a new key to create another",
    )


async def read_body(request: Request, limit: int) -> bytearray:
    """Read the request body, refusing it with 413 once it passes ``limit`` bytes.

    A declared ``Content-Length`` over the limit is refused before anything is
    read; otherwise the stream is counted as it arrives, which also bounds a body
    sent without a length.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise too_large(limit)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise too_large(limit)
    return body


def too_large(limit: int) -> HTTPException:
    """The 413 for a batch body over the size limit."""
    return HTTPException(
        413,
        f"Request body exceeds the batch size limit of {limit // (1024 * 1024)} MiB; "
        "split the requests into several batches",
    )


def has_invalid_unicode(value: Any) -> bool:
    r"""Whether a decoded JSON value holds a string that is not valid Unicode.

    JSON allows escaped lone surrogates (a ``\u`` escape of U+D800); Python decodes them
    into strings that cannot be encoded, stored or rendered. Found anywhere --
    a key, a value, deep in a body -- the request is refused up front rather
    than failing later in the database or a result file.
    """
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            try:
                current.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def reject_json_constant(name: str) -> Any:
    """``parse_constant`` refusing ``NaN`` and ``Infinity`` (not JSON; Postgres refuses them)."""
    raise ValueError(f"{name} is not valid JSON")


async def precheck_active(store: Any, owner: str, config: BatchConfig) -> None:
    """Refuse early (before validating a large body) when the owner is at the active cap."""
    active = await store.count_active_batches(owner)
    if active >= config.max_active_per_user:
        raise limit_error(
            BatchLimitExceeded("active_batches", limit=config.max_active_per_user, current=active)
        )


def limit_error(exc: BatchLimitExceeded) -> HTTPException:
    """The 429 for a per-user cap."""
    if exc.kind == "active_batches":
        return HTTPException(
            429,
            f"Too many unfinished batches: at most {exc.limit} may be queued or running at "
            "once; wait for one to finish or cancel one",
        )
    return HTTPException(
        429,
        f"Too many unfinished batch requests: you have {exc.current} and this batch adds "
        f"{exc.requested}, over the limit of {exc.limit}; wait for some to finish",
    )


async def create(store: Any, **kwargs: Any) -> dict[str, Any]:
    """Run the create transaction, answering its refusals as HTTP errors."""
    owner = kwargs.get("user_id")
    try:
        return await store.create_batch(**kwargs)
    except BatchOwnerUnavailable:
        # Authentication succeeded before the body was validated, but the
        # account may have been suspended or hard-deleted before this
        # transaction locked it. Present the same answer as the auth path.
        raise HTTPException(401, "Invalid or expired API key") from None
    except BatchLimitExceeded as exc:
        raise limit_error(exc) from None
    except IdempotencyConflict:
        raise _conflict() from None
    except IdempotencyTombstoned:
        raise _gone() from None
    except ValueError as exc:
        # The store refuses what it cannot hold (a repeated or unstorable
        # custom_id). Validation catches those first; this keeps a 400, with
        # static text, since a response never carries exception text.
        logger.warning("Batch store refused a batch: %s", exc, extra={"user_id": owner})
        raise HTTPException(
            400, "Invalid batch: a custom_id is repeated or holds a character that cannot be stored"
        ) from None


def wake_worker(services: Any) -> None:
    """Tell this process's worker a batch is waiting, so it need not wait for its next poll."""
    scheduler = getattr(services, "batch_scheduler", None)
    if scheduler is None:
        return
    try:
        scheduler.notify()
    except Exception:
        # The batch is stored either way and the worker polls; failing the create
        # now would only invite the caller to submit it twice.
        logger.warning("Could not wake the batch worker", exc_info=True)
