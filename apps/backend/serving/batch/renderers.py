r"""Batch result lines, and the status a batch shows.

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

Every batch, whatever its target, uses the OpenAI batch line shape; a
Messages item's ``body`` is the Messages response as the handler returned it.

- Output file (items ``completed``): ``{"id", "custom_id", "response":
  {"status_code", "request_id", "body"}, "error": null}``.
- Error file (every other final item): with an HTTP status, ``"response":
  {"status_code", "request_id", "body": {"error": {"message", "type", "param",
  "code"}}}, "error": null``; without one (cancelled, expired, the batch
  failed), ``"response": null, "error": {"code", "message"}``.
- Inline export: the line of each item, output and error alike, in one file.
  Batches the retired Message Batches API stored are exported the same way.
"""

from __future__ import annotations

import json
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

#: Item statuses whose line goes to the output file; every other final
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


def result_line(item: dict[str, Any], version: int = RENDERER_VERSION) -> bytes:
    """The batch output (or error) line of one final item."""
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


def _check(version: int) -> None:
    if version not in SUPPORTED_RENDERER_VERSIONS:
        raise ValueError(f"unknown batch renderer version {version}")


# -- status -------------------------------------------------------------------------


def display_status(row: dict[str, Any]) -> str:
    """The status a batch row shows, from its execution status and phase.

    The same for every target. A file batch shows ``validating`` until its
    input is checked, ``finalizing`` while its output files are sealed
    (``cancelling`` when it was cancelled), and its final status only once they
    are published; anything still finishing its running requests after an
    expiry or failure shows ``in_progress`` until then. Any other batch -- an
    inline one, or one the retired Message Batches API stored -- shows its
    execution status. ``serving.storage.batch_store.display_status_sql`` is the
    same rule in SQL, for filtering by status.
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
