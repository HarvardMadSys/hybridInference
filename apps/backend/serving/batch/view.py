"""The batch view both surfaces return: ``GET /v1/batches`` and the webhook payload.

Keeping the mapping here means the completion callback carries exactly the batch
object the caller would have polled for, field for field.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from serving.batch.schemas import (
    CHAT_COMPLETIONS_URL,
    BatchObject,
    BatchRequestCounts,
    BatchWebhookState,
)
from serving.utils.secret_urls import url_fingerprint

if TYPE_CHECKING:
    from serving.batch.config import BatchConfig


def epoch(value: Any) -> int | None:
    """Unix seconds for a timestamp, or None."""
    return int(value.timestamp()) if isinstance(value, datetime) else None


def completion_window(row: dict[str, Any], config: BatchConfig) -> str:
    """Return the window the batch was created with, as OpenAI spells it (``24h``).

    Read off the batch's own timestamps rather than the live setting, which may
    have changed since.
    """
    created, expires = row.get("created_at"), row.get("expires_at")
    if isinstance(created, datetime) and isinstance(expires, datetime):
        hours = max(1, round((expires - created).total_seconds() / 3600))
    else:
        hours = config.completion_window_hours
    return f"{hours}h"


def webhook_state(row: dict[str, Any]) -> BatchWebhookState | None:
    """The batch's callback state, or None when it has no webhook."""
    url = row.get("webhook_url")
    status = row.get("webhook_status")
    if not url or status in (None, "none"):
        return None
    return BatchWebhookState(
        url_fingerprint=url_fingerprint(url),
        status=status,
        attempts=int(row.get("webhook_attempts") or 0),
        last_status_code=row.get("webhook_last_status_code"),
        last_error=row.get("webhook_last_error"),
        last_attempt_at=epoch(row.get("webhook_last_attempt_at")),
        delivered_at=epoch(row.get("webhook_delivered_at")),
    )


def batch_object(row: dict[str, Any], config: BatchConfig) -> BatchObject:
    """Map a store row to the API object.

    Field by field, so the row's ownership columns (``user_id``, ``key_hash``)
    and the webhook URL can never reach a response.
    """
    return BatchObject(
        id=row["id"],
        endpoint=row.get("endpoint") or CHAT_COMPLETIONS_URL,
        completion_window=completion_window(row, config),
        status=row["status"],
        created_at=epoch(row.get("created_at")) or 0,
        in_progress_at=epoch(row.get("in_progress_at")),
        expires_at=epoch(row.get("expires_at")),
        completed_at=epoch(row.get("completed_at")),
        failed_at=epoch(row.get("failed_at")),
        expired_at=epoch(row.get("expired_at")),
        cancelling_at=epoch(row.get("cancelling_at")),
        cancelled_at=epoch(row.get("cancelled_at")),
        request_counts=BatchRequestCounts(
            total=row.get("request_count") or 0,
            completed=row.get("completed_count") or 0,
            failed=row.get("failed_count") or 0,
        ),
        metadata=row.get("metadata"),
        cost_usd=float(row.get("cost_usd") or 0.0),
        error=row.get("error"),
        webhook=webhook_state(row),
    )
