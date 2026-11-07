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
  does not parse, or does not match the schema, counts as a miss. The schema
  is the caller's and the check runs on the event loop, so it is bounded: the
  caller's regexes are not run, references resolve only inside the schema, and
  a check that would take too long holds the reply only to being JSON;
* a miss is asked again, with the schema in the prompt
  (``with_instruction``), a bounded number of times before the last reply is
  returned as it is.
"""

from __future__ import annotations

import copy
import functools
import json
import math
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from jsonschema.validators import create as create_validator_class
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)

#: Further attempts for a reply that misses its ``response_format``. Each is a
#: full upstream call, so the count stays small.
MAX_STRUCTURED_RETRIES = 2

_JSON_TYPES = ("json_schema", "json_object")

#: The longest schema, as JSON text, the gateway holds a reply to. A longer one
#: holds it only to being JSON, as an invalid schema does: every reply is
#: checked against the schema on the event loop, and the schema is the
#: caller's. Generous: a structured-output schema is usually a few KiB.
MAX_CHECKED_SCHEMA_CHARS = 64 * 1024

#: Event-loop time one reply check may take. A typical schema and reply take
#: well under a millisecond, but a schema can make jsonschema's work grow
#: exponentially (an ``allOf`` of two ``$ref`` to the next level, thirty levels
#: deep, is 2**30 steps), so a check that runs out of time stops and holds the
#: reply only to being JSON.
_CHECK_SECONDS = 0.05

#: The longest description of a schema mismatch a check keeps, for the log.
_MAX_REASON_CHARS = 300


@dataclass(frozen=True)
class RequestedFormat:
    """What a request's ``response_format`` asks for.

    ``schema`` is the JSON Schema of a ``json_schema`` request, ``None`` for
    ``json_object`` (any JSON object) or a ``json_schema`` sent without one.
    """

    kind: str
    schema: dict[str, Any] | None = None

    @functools.cached_property
    def _checked_schema_text(self) -> str | None:
        """The schema as canonical JSON text, if replies are held to it.

        None without a schema, or for one longer than
        :data:`MAX_CHECKED_SCHEMA_CHARS`. Computed once per request, which is
        checked up to ``1 + MAX_STRUCTURED_RETRIES`` times.
        """
        if not self.schema:
            return None
        try:
            text = json.dumps(self.schema, sort_keys=True)
        except (TypeError, ValueError, RecursionError):
            return None
        if len(text) > MAX_CHECKED_SCHEMA_CHARS:
            logger.info(
                "structured_output_unchecked",
                extra={
                    "event": "structured_output_unchecked",
                    "reason": f"schema of {len(text)} characters",
                },
            )
            return None
        return text


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
    mismatch = _schema_mismatch(value, wanted)
    if mismatch is not None:
        return ReplyCheck(ok=False, reason=f"reply does not match the schema: {mismatch}")
    if _is_plain_json(content):
        return ReplyCheck(ok=True, content=content)
    # Fenced or wrapped in prose: return just the JSON the caller asked for.
    return ReplyCheck(ok=True, content=json.dumps(value, ensure_ascii=False))


def _schema_mismatch(value: Any, wanted: RequestedFormat) -> str | None:
    """Describe how *value* misses the requested schema, or return None.

    None as well when the reply cannot be held to the schema: there is none,
    it is too long or invalid, or applying it fails or runs out of time. None
    of that is the reply's fault, so the reply is then held only to being
    JSON.
    """
    text = wanted._checked_schema_text
    validator = _validator(text) if text is not None else None
    if validator is None:
        return None
    token = _check_deadline.set(time.monotonic() + _CHECK_SECONDS)
    try:
        error = next(iter(validator.iter_errors(value)), None)
    except Exception as exc:
        # The schema is the caller's: whatever applying it does -- run out of
        # time, recurse too deep, point at nothing -- is not the reply's fault.
        logger.info(
            "structured_output_unchecked",
            extra={
                "event": "structured_output_unchecked",
                "reason": str(exc) if isinstance(exc, _Unchecked) else type(exc).__name__,
            },
        )
        return None
    finally:
        _check_deadline.reset(token)
    if error is None:
        return None
    message = error.message
    if len(message) > _MAX_REASON_CHARS:
        message = message[: _MAX_REASON_CHARS - 1] + "…"
    return message


class _Unchecked(Exception):
    """Applying the schema to a reply stopped before it reached a verdict."""


#: When the reply check in progress has to stop (``time.monotonic``).
_check_deadline: ContextVar[float] = ContextVar(
    "structured_output_check_deadline", default=math.inf
)


def _require_time_left() -> None:
    """Raise ``_Unchecked`` once the reply check in progress has run out of time."""
    if time.monotonic() > _check_deadline.get():
        raise _Unchecked("the check ran out of time")


def _keywords_while_time_is_left(schema: Any) -> Any:
    """Return a schema object's keywords for jsonschema to apply, while time is left.

    jsonschema asks for them each time it applies a schema object to a part of
    the reply -- every step of a check, an empty subschema included -- so a
    check that runs out of time stops at its next step.

    Raises:
        _Unchecked: The check's time is up.
    """
    _require_time_left()
    return schema.items()


def _not_applied(validator: Any, value: Any, instance: Any, schema: Any) -> Any:
    """A keyword the gateway's check does not apply; see :data:`_KEYWORDS`."""
    return ()


def _additional_properties(validator: Any, value: Any, instance: Any, schema: Any) -> Any:
    """``additionalProperties``, unless ``patternProperties`` decides what is additional.

    jsonschema matches the property names against those regexes to find the
    additional ones, and the gateway does not run them.
    """
    if "patternProperties" in schema:
        return ()
    return _STOCK_KEYWORDS["additionalProperties"](validator, value, instance, schema)


def _unique_items(validator: Any, unique: Any, instance: Any, schema: Any) -> Any:
    """``uniqueItems`` in linear time.

    jsonschema compares every pair of items it cannot sort, objects among
    them: 4,000 objects took 12.6 s. Items are compared by their canonical
    JSON instead, which tells ``1`` from ``1.0`` where jsonschema does not --
    a duplicate missed, never one invented.
    """
    if not unique or not validator.is_type(instance, "array"):
        return
    seen: set[str] = set()
    for item in instance:
        _require_time_left()
        key = json.dumps(item, sort_keys=True, allow_nan=False)
        if key in seen:
            yield ValidationError("the array has non-unique elements")
            return
        seen.add(key)


def _is_schema(value: Any) -> bool:
    return isinstance(value, dict | bool)


def _is_schema_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(map(_is_schema, value))


def _is_schema_map(value: Any) -> bool:
    return isinstance(value, dict) and all(map(_is_schema, value.values()))


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_count(value: Any) -> bool:
    if isinstance(value, float):
        return value.is_integer() and value >= 0
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


#: The shape of each keyword's value in the draft 2020-12 meta-schema. A
#: keyword whose value has another shape makes the schema invalid, which is not
#: the reply's fault: the reply is then held only to being JSON. Checked as
#: each keyword is applied, which costs nothing up front -- jsonschema's
#: ``check_schema`` takes a quarter of a millisecond per subschema (0.25 s for
#: 1,000 properties) -- and a malformed part the reply never reaches does not
#: count. Most other malformed values fail as they are applied, which ends the
#: same way.
_KEYWORD_SHAPES: dict[str, Callable[[Any], bool]] = {
    "$ref": lambda value: isinstance(value, str),
    "$dynamicRef": lambda value: isinstance(value, str),
    "additionalProperties": _is_schema,
    "contains": _is_schema,
    "if": _is_schema,
    "items": _is_schema,
    "not": _is_schema,
    "propertyNames": _is_schema,
    "allOf": _is_schema_list,
    "anyOf": _is_schema_list,
    "oneOf": _is_schema_list,
    "prefixItems": _is_schema_list,
    "properties": _is_schema_map,
    "dependentSchemas": _is_schema_map,
    "dependentRequired": lambda value: (
        isinstance(value, dict) and all(map(_is_string_list, value.values()))
    ),
    "required": _is_string_list,
    "enum": lambda value: isinstance(value, list),
    "type": lambda value: isinstance(value, str) or (bool(value) and _is_string_list(value)),
    "maximum": _is_number,
    "minimum": _is_number,
    "exclusiveMaximum": _is_number,
    "exclusiveMinimum": _is_number,
    "multipleOf": lambda value: _is_number(value) and value > 0,
    "maxItems": _is_count,
    "minItems": _is_count,
    "maxLength": _is_count,
    "minLength": _is_count,
    "maxProperties": _is_count,
    "minProperties": _is_count,
    "uniqueItems": lambda value: isinstance(value, bool),
}


def _shape_checked(name: str, keyword: Callable[..., Any]) -> Callable[..., Any]:
    fits = _KEYWORD_SHAPES[name]

    def apply(validator: Any, value: Any, instance: Any, schema: Any) -> Any:
        if not fits(value):
            raise _Unchecked(f"the schema's {name!r} is invalid")
        return keyword(validator, value, instance, schema)

    return apply


_STOCK_KEYWORDS = Draft202012Validator.VALIDATORS

#: The keywords the gateway applies: draft 2020-12's, except
#:
#: * ``pattern`` and ``patternProperties``, which would run the caller's
#:   regexes with Python's backtracking ``re`` -- ``^(a+)+$`` against 40
#:   characters takes hours -- and ``additionalProperties`` beside
#:   ``patternProperties``, which depends on them;
#: * ``unevaluatedItems`` and ``unevaluatedProperties``, which jsonschema works
#:   out with list lookups, quadratic in the reply (1.3 s for 20,000 items), and
#:   for properties with those regexes too. They are rare in structured output.
#:
#: ``format`` is an annotation: no format checker is given, so no format,
#: ``regex`` included, is checked.
_KEYWORDS: dict[str, Callable[..., Any]] = {
    name: _shape_checked(name, keyword) if name in _KEYWORD_SHAPES else keyword
    for name, keyword in {
        **_STOCK_KEYWORDS,
        "pattern": _not_applied,
        "patternProperties": _not_applied,
        "additionalProperties": _additional_properties,
        "unevaluatedItems": _not_applied,
        "unevaluatedProperties": _not_applied,
        "uniqueItems": _unique_items,
    }.items()
}

_GatewayValidator = create_validator_class(
    meta_schema=Draft202012Validator.META_SCHEMA,
    validators=_KEYWORDS,
    type_checker=Draft202012Validator.TYPE_CHECKER,
    id_of=Draft202012Validator.ID_OF,
    applicable_validators=_keywords_while_time_is_left,
)


@functools.lru_cache(maxsize=64)
def _validator(schema_text: str) -> Any | None:
    """Return the gateway's validator for a schema, or None when it holds replies only to JSON.

    Keyed by the schema's JSON text: a batch sends one schema thousands of
    times. The schema's own ``$schema`` is dropped -- the check is draft
    2020-12 with the gateway's keywords -- and a schema that names a dialect
    anywhere else is not used: jsonschema switches to its stock validator, and
    so to the keywords the gateway does not apply, for a schema object that
    names one. References resolve only inside the schema: jsonschema would
    fetch a remote ``$ref`` itself, from wherever it points, on the event loop
    and with no timeout.
    """
    schema = json.loads(schema_text)
    if isinstance(schema, dict):
        schema.pop("$schema", None)
    if '"$schema"' in json.dumps(schema):
        logger.info(
            "structured_output_unchecked",
            extra={"event": "structured_output_unchecked", "reason": "schema names a dialect"},
        )
        return None
    try:
        resolver = Registry().resolver_with_root(DRAFT202012.create_resource(schema))
        return _GatewayValidator(schema, format_checker=None, _resolver=resolver)
    except Exception as exc:
        logger.info(
            "structured_output_unchecked",
            extra={"event": "structured_output_unchecked", "reason": type(exc).__name__},
        )
        return None


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
