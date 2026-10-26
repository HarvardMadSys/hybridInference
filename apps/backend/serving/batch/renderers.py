r"""Batch objects and result lines, as each protocol spells them.

Result files are **virtual**: nothing but the batch's items is stored, and a
download renders the lines again from them. That is only sound if rendering
the same items always produces the same bytes, so a renderer is versioned and
frozen: :data:`RENDERER_VERSION` names the one below, an output manifest records
the version it was sealed with, and a download uses that version. A change to
any line format must add a new version and keep this one for the manifests
already sealed with it.

Version 1, for every line: the JSON object below, with its keys in the order
written here, serialized with ``ensure_ascii=False`` and no whitespace
(``separators=(",", ":")``), encoded as UTF-8 and followed by ``\n``. Nested
response bodies are emitted as the store returns them (PostgreSQL ``jsonb``
gives a deterministic key order for a stored value). Items go in submission
order (by item id).

- OpenAI output file (items ``completed``): ``{"id", "custom_id", "response":
  {"status_code", "request_id", "body"}, "error": null}``.
- OpenAI error file (every other final item): with an HTTP status, ``"response":
  {"status_code", "request_id", "body": {"error": {"message", "type", "param",
  "code"}}}, "error": null``; without one (cancelled, expired, the batch
  failed), ``"response": null, "error": {"code", "message"}``.
- Anthropic results: ``{"custom_id", "result": {...}}``, the result one of
  ``{"type": "succeeded", "message"}``, ``{"type": "errored", "error":
  {"type": "error", "error": {"type", "message"}, "request_id"}}``,
  ``{"type": "canceled"}`` and ``{"type": "expired"}``.
- Inline export: the OpenAI line of each item, output and error alike, in one
  file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from serving.batch.schemas import RESULT_ID_PREFIX

RENDERER_VERSION = 1
SUPPORTED_RENDERER_VERSIONS = frozenset({1})

# Error codes as the OpenAI error body names their type.
_OPENAI_ERROR_TYPES: dict[str, str] = {
    "invalid_request": "invalid_request_error",
    "model_not_found": "invalid_request_error",
    "forbidden": "permission_error",
    "rate_limited": "rate_limit_error",
    "timeout": "timeout_error",
    "service_unavailable": "server_error",
    "upstream_error": "server_error",
    "result_too_large": "server_error",
}
# Error codes as the Anthropic error object names their type.
_ANTHROPIC_ERROR_TYPES: dict[str, str] = {
    "invalid_request": "invalid_request_error",
    "model_not_found": "not_found_error",
    "forbidden": "permission_error",
    "rate_limited": "rate_limit_error",
    "timeout": "timeout_error",
    "service_unavailable": "overloaded_error",
    "upstream_error": "api_error",
    "result_too_large": "api_error",
    "owner_unauthorized": "authentication_error",
    "batch_failed": "api_error",
}

#: Item statuses whose line goes to the OpenAI output file; every other final
#: status goes to the error file.
OUTPUT_STATUSES = frozenset({"completed"})
FINAL_ITEM_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})


def _encode(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _item_error(item: dict[str, Any]) -> dict[str, Any]:
    error = item.get("error")
    if isinstance(error, dict):
        return error
    if item.get("status") == "cancelled":
        return {
            "code": "batch_cancelled",
            "message": "The batch was cancelled before this request ran.",
        }
    if item.get("status") == "expired":
        return {
            "code": "batch_expired",
            "message": "The batch's completion window ended before this request ran.",
        }
    return {"code": "upstream_error", "message": "The request failed."}


def openai_line(item: dict[str, Any], version: int = RENDERER_VERSION) -> bytes:
    """The OpenAI batch output (or error) line of one final item."""
    _check(version)
    line_id = f"{RESULT_ID_PREFIX}{item['id']}"
    if item.get("status") in OUTPUT_STATUSES:
        return _encode(
            {
                "id": line_id,
                "custom_id": item["custom_id"],
                "response": {
                    "status_code": item.get("status_code") or 200,
                    "request_id": item.get("request_id"),
                    "body": item.get("response"),
                },
                "error": None,
            }
        )
    error = _item_error(item)
    code = str(error.get("code") or "upstream_error")
    message = str(error.get("message") or "")
    status = item.get("status_code") or error.get("status")
    if item.get("status") == "failed" and isinstance(status, int):
        return _encode(
            {
                "id": line_id,
                "custom_id": item["custom_id"],
                "response": {
                    "status_code": status,
                    "request_id": item.get("request_id"),
                    "body": {
                        "error": {
                            "message": message,
                            "type": _OPENAI_ERROR_TYPES.get(code, "server_error"),
                            "param": None,
                            "code": code,
                        }
                    },
                },
                "error": None,
            }
        )
    if item.get("status") == "cancelled":
        code = "batch_cancelled"
        message = message or "The batch was cancelled before this request ran."
    return _encode(
        {
            "id": line_id,
            "custom_id": item["custom_id"],
            "response": None,
            "error": {"code": code, "message": message},
        }
    )


def anthropic_line(item: dict[str, Any], version: int = RENDERER_VERSION) -> bytes:
    """The Message Batch results line of one final item."""
    _check(version)
    status = item.get("status")
    if status == "completed":
        result: dict[str, Any] = {"type": "succeeded", "message": item.get("response")}
    elif status == "cancelled":
        result = {"type": "canceled"}
    elif status == "expired":
        result = {"type": "expired"}
    else:
        error = _item_error(item)
        native = error.get("anthropic") if isinstance(error.get("anthropic"), dict) else None
        if native is not None:
            inner = {
                "type": native.get("type") or "api_error",
                "message": native.get("message") or "",
            }
        else:
            code = str(error.get("code") or "upstream_error")
            inner = {
                "type": _ANTHROPIC_ERROR_TYPES.get(code, "api_error"),
                "message": str(error.get("message") or ""),
            }
        result = {
            "type": "errored",
            "error": {"type": "error", "error": inner, "request_id": item.get("request_id")},
        }
    return _encode({"custom_id": item["custom_id"], "result": result})


def _check(version: int) -> None:
    if version not in SUPPORTED_RENDERER_VERSIONS:
        raise ValueError(f"unknown batch renderer version {version}")


# -- batch objects ------------------------------------------------------------------


def _epoch(value: Any) -> int | None:
    return int(value.timestamp()) if isinstance(value, datetime) else None


def _rfc3339(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def openai_status(row: dict[str, Any]) -> str:
    """The OpenAI status of a batch row, from its execution status and phase.

    An inline batch stored before phases existed keeps the status it had. A
    file batch shows ``finalizing`` while its output files are sealed
    (``cancelling`` when it was cancelled), and its final status only once they
    are published. Anything still finishing its running requests after an
    expiry or failure shows ``in_progress`` until then.
    """
    status = row["status"]
    phase = row.get("phase") or "runnable"
    if row.get("api_surface") != "openai_file":
        return status
    if phase == "validating":
        return "cancelling" if status == "cancelling" else "validating"
    if phase == "sealing":
        return "cancelling" if row.get("pending_terminal_status") == "cancelled" else "finalizing"
    if phase == "sealed":
        return status
    if status in ("expired", "failed"):
        return "in_progress"
    return status


def anthropic_processing_status(row: dict[str, Any]) -> str:
    """``in_progress``, ``canceling`` or ``ended``, from a batch row."""
    if row.get("phase") == "sealed":
        return "ended"
    if row.get("status") == "cancelling" or row.get("pending_terminal_status") == "cancelled":
        return "canceling"
    return "in_progress"


def anthropic_request_counts(row: dict[str, Any]) -> dict[str, int]:
    """Anthropic's tallies: every request counts as processing until the batch ends."""
    total = int(row.get("request_count") or 0)
    if row.get("phase") != "sealed":
        return {"processing": total, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0}
    expired = int(row.get("expired_count") or 0)
    canceled = int(row.get("cancelled_count") or 0)
    succeeded = int(row.get("completed_count") or 0)
    errored = max(0, int(row.get("failed_count") or 0) - expired)
    return {
        "processing": max(0, total - succeeded - errored - canceled - expired),
        "succeeded": succeeded,
        "errored": errored,
        "canceled": canceled,
        "expired": expired,
    }


def anthropic_batch(row: dict[str, Any], *, results_base: str) -> dict[str, Any]:
    """The ``message_batch`` object of a batch row."""
    ended = row.get("phase") == "sealed"
    return {
        "id": row["id"],
        "type": "message_batch",
        "processing_status": anthropic_processing_status(row),
        "request_counts": anthropic_request_counts(row),
        "ended_at": _rfc3339(row.get("sealed_at")) if ended else None,
        "created_at": _rfc3339(row.get("created_at")),
        "expires_at": _rfc3339(row.get("expires_at")),
        "archived_at": None,
        "cancel_initiated_at": _rfc3339(row.get("cancelling_at")),
        "results_url": f"{results_base}/{row['id']}/results" if ended else None,
    }
