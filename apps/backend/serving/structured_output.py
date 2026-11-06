"""Structured-output (``response_format``) checks the gateway applies itself.

A caller that sends ``response_format`` asks for a JSON reply, but whether it
gets one depends on the endpoint that serves the request: vLLM and SGLang
constrain decoding to the schema, some remote APIs honor only ``json_object``
or nothing at all, and a model whose route cannot take ``response_format``
never sees it. The gateway cannot make every endpoint enforce a schema, so it
tolerates the ones that do not:

* an adapter that cannot forward the schema describes it in the prompt instead
  (``instruction_text``), so the model is at least asked for the right shape;
* a reply is checked against the request (``check_reply``): JSON wrapped in a
  code fence or surrounded by prose is unwrapped, and only a reply that still
  does not parse, or does not match the schema, counts as a miss;
* a miss is asked again, with the schema in the prompt
  (``with_instruction``), a bounded number of times before the last reply is
  returned as it is.
"""

from __future__ import annotations

import copy
import functools
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)

#: Further attempts for a reply that misses its ``response_format``. Each is a
#: full upstream call, so the count stays small.
MAX_STRUCTURED_RETRIES = 2

_JSON_TYPES = ("json_schema", "json_object")


@dataclass(frozen=True)
class RequestedFormat:
    """What a request's ``response_format`` asks for.

    ``schema`` is the JSON Schema of a ``json_schema`` request, ``None`` for
    ``json_object`` (any JSON object) or a ``json_schema`` sent without one.
    """

    kind: str
    schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReplyCheck:
    """The outcome of checking one reply against its requested format.

    ``content`` is the reply to return when ``ok``: the JSON text itself, with
    any code fence or surrounding prose removed. ``reason`` says why a reply
    failed, for the log.
    """

    ok: bool
    content: str | None = None
    reason: str | None = None


def requested_format(params: dict[str, Any]) -> RequestedFormat | None:
    """Return the JSON format a request's params ask for, or None for text."""
    response_format = params.get("response_format")
    if not isinstance(response_format, dict):
        return None
    kind = response_format.get("type")
    if kind not in _JSON_TYPES:
        return None
    schema = None
    if kind == "json_schema":
        spec = response_format.get("json_schema")
        if isinstance(spec, dict) and isinstance(spec.get("schema"), dict):
            schema = spec["schema"]
        elif isinstance(response_format.get("schema"), dict):
            schema = response_format["schema"]
    return RequestedFormat(kind=kind, schema=schema)


def _extract_json(text: str) -> Any:
    """Return the first JSON value in *text*, tolerating fences and prose.

    Raises ``ValueError`` when no JSON value can be found.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # ```json\n{...}\n``` -- drop the fence lines.
        lines = stripped.splitlines()
        if len(lines) >= 2:
            body = lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
            stripped = "\n".join(body).strip()
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char in "{[":
            try:
                value, _ = decoder.raw_decode(stripped, index)
            except ValueError:
                continue
            return value
    raise ValueError("no JSON value in the reply")


def check_reply(content: Any, wanted: RequestedFormat) -> ReplyCheck:
    """Check one reply's text against the format the request asked for."""
    if not isinstance(content, str) or not content.strip():
        return ReplyCheck(ok=False, reason="empty reply")
    try:
        value = _extract_json(content)
    except ValueError:
        return ReplyCheck(ok=False, reason="reply is not JSON")
    if wanted.kind == "json_object" and not isinstance(value, dict):
        return ReplyCheck(ok=False, reason="reply is not a JSON object")
    validator = _validator(json.dumps(wanted.schema, sort_keys=True)) if wanted.schema else None
    if validator is not None:
        error = next(iter(validator.iter_errors(value)), None)
        if error is not None:
            return ReplyCheck(ok=False, reason=f"reply does not match the schema: {error.message}")
    if _is_plain_json(content):
        return ReplyCheck(ok=True, content=content)
    # Fenced or wrapped in prose: return just the JSON the caller asked for.
    return ReplyCheck(ok=True, content=json.dumps(value, ensure_ascii=False))


@functools.lru_cache(maxsize=256)
def _validator(schema_json: str) -> Draft202012Validator | None:
    """Return a validator for a schema, or None when the schema is itself invalid.

    Keyed by the schema's JSON text: a batch sends one schema thousands of
    times. An invalid schema leaves nothing to hold the reply to beyond being
    JSON, which is not the reply's fault.
    """
    schema = json.loads(schema_json)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        return None
    return Draft202012Validator(schema)


def _is_plain_json(content: str) -> bool:
    """Whether *content* is exactly one JSON value, whitespace aside."""
    try:
        json.loads(content)
    except ValueError:
        return False
    return True


def instruction_text(wanted: RequestedFormat) -> str:
    """Return the prompt text that asks the model for the requested format."""
    if wanted.schema is None:
        return "Respond with a single JSON object only, with no other text."
    schema = json.dumps(wanted.schema, ensure_ascii=False, separators=(",", ":"))
    return (
        "Respond with a single JSON value that matches this JSON Schema, "
        f"with no other text:\n{schema}"
    )


def with_instruction(
    messages: list[dict[str, Any]], wanted: RequestedFormat
) -> list[dict[str, Any]]:
    """Return *messages* with the format instruction added to the last user turn.

    The instruction goes into the user turn rather than a system message
    because some chat templates refuse a system role or a second system
    message. The caller's list is not modified.
    """
    text = instruction_text(wanted)
    updated = copy.deepcopy(messages)
    for message in reversed(updated):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = f"{content}\n\n{text}"
        elif isinstance(content, list):
            message["content"] = [*content, {"type": "text", "text": text}]
        else:
            continue
        return updated
    return [*updated, {"role": "user", "content": text}]


def reply_content(response: Any) -> tuple[Any, str | None, bool]:
    """Return ``(content, finish_reason, has_tool_calls)`` of a chat response."""
    if not isinstance(response, dict):
        return None, None, False
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, None, False
    choice = choices[0]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    return message.get("content"), choice.get("finish_reason"), bool(message.get("tool_calls"))


def set_reply_content(response: dict[str, Any], content: str) -> None:
    """Replace the first choice's message content in place."""
    response["choices"][0]["message"]["content"] = content


def worth_retrying(response: Any) -> bool:
    """Whether another attempt could produce a conforming reply.

    A reply that called tools is not a format miss. A reply that ran out of
    tokens -- spent on reasoning, or cut off partway through the JSON -- runs
    out the same way next time on the same ``max_tokens``.
    """
    _, finish_reason, has_tool_calls = reply_content(response)
    return not has_tool_calls and finish_reason != "length"


async def conform_reply(
    response: Any,
    wanted: RequestedFormat,
    retry: Callable[[list[dict[str, Any]]], Awaitable[Any]],
    messages: list[dict[str, Any]],
) -> tuple[Any, dict[str, Any]]:
    """Return a reply that matches *wanted* if one can be had, and a log summary.

    *response* is the first attempt's reply. A miss is asked again through
    *retry* -- the same routed call, so another endpoint may answer -- with the
    format spelled out in the prompt, up to ``MAX_STRUCTURED_RETRIES`` times.
    When no attempt conforms, or a retry fails outright, the last reply the
    caller could have been given is returned unchanged: the gateway never
    turns a reply into an error just because it is not the requested shape.
    """
    attempts = 1
    check = check_reply(reply_content(response)[0], wanted)
    while not check.ok and attempts <= MAX_STRUCTURED_RETRIES and worth_retrying(response):
        logger.info(
            "structured_output_retry",
            extra={"event": "structured_output_retry", "attempt": attempts, "reason": check.reason},
        )
        try:
            candidate = await retry(with_instruction(messages, wanted))
        except Exception as exc:
            logger.info(
                "structured_output_retry_failed",
                extra={"event": "structured_output_retry_failed", "error": type(exc).__name__},
            )
            break
        attempts += 1
        response = candidate
        check = check_reply(reply_content(response)[0], wanted)
    if (
        check.ok
        and check.content is not None
        and isinstance(response, dict)
        and check.content != reply_content(response)[0]
    ):
        set_reply_content(response, check.content)
    summary = {"attempts": attempts, "conforms": check.ok}
    if not check.ok:
        summary["reason"] = check.reason
    return response, summary
