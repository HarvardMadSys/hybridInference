"""Checking a console batch's source file: identify it, preflight it, and prepare what is stored.

The console creates a batch from a JSONL file the owner uploaded (the
*source*). Before anything is created it reads the whole file here, twice,
streaming it a few chunks at a time under a read lease, never holding it in
memory and never calling a model:

1. **Identify** -- every line is parsed strictly (no byte-order mark, no key
   repeated within an object, no ``NaN``/``Infinity``, valid Unicode) and its
   envelope checked (``custom_id`` unique, ``method`` POST when given, no field
   the worker would ignore). Its type comes from its ``url`` when it has one
   -- only ``/v1/chat/completions`` and ``/v1/embeddings`` are created here --
   and otherwise from a body shape only one type has (``messages`` without
   ``input``: Chat; ``input`` without ``messages``: Embeddings). The file is
   one type: a ``url`` that contradicts its body, or rows of both types, are
   errors no choice can override. Only when no row says what the file is does
   the owner pick the type, and that choice then only fills the missing
   ``url``.
2. **Validate** (with the type known) -- every body is checked strictly
   against the request model of its endpoint (no coercion; every field the
   model does not declare is ``unsupported_parameter``, since the gateway
   would drop it silently; free-form fields such as ``tools`` and a JSON
   schema are not looked into), the overrides are applied (model, and for
   Chat ``temperature`` and ``max_tokens``), and the model is admitted as the
   worker will admit it, for the account's own model access. Every row must
   end on one canonical model, which the stored request names.

A submission does the same and, when what is stored differs from the source
(an override, a filled ``url``, a model alias pinned to its canonical id),
writes the stored requests to a *derived* upload, which only the create
transaction makes usable (:meth:`BatchStore.create_console_batch`).

Errors name the line, the ``custom_id`` and the field, never the content of a
request. At most :data:`MAX_ERRORS` are listed, with the total. Scans are
bounded per process (:data:`MAX_SCANS`, one per user) and in time
(:data:`SCAN_TIMEOUT_SECONDS`); see
``docs/agents/specs/2026-10-28-batch-ui-create-design.zh.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import math
import types
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from routing.model_selection import is_auto_model_id
from serving.batch.admission import has_invalid_unicode
from serving.batch.delivery import READ_LEASE_SECONDS, LeaseKeeper
from serving.batch.targets import TARGET_URLS, ItemRejected, resolve_model
from serving.batch.validation import LineTooLong, iter_lines
from serving.schemas import ChatCompletionRequest, EmbeddingRequest
from serving.storage.batch_file_store import CHUNK_BYTES
from serving.storage.batch_store import TARGET_CHAT, TARGET_EMBEDDINGS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from serving.batch.config import BatchConfig
    from serving.batch.targets import AdmissionContext

#: The types the console creates, by the ``url`` that names each.
CONSOLE_TARGETS: dict[str, str] = {
    "/v1/chat/completions": TARGET_CHAT,
    "/v1/embeddings": TARGET_EMBEDDINGS,
}
#: The ``url`` of each console type.
TARGET_URL: dict[str, str] = {target: url for url, target in CONSOLE_TARGETS.items()}
_REQUEST_MODELS: dict[str, type[BaseModel]] = {
    TARGET_CHAT: ChatCompletionRequest,
    TARGET_EMBEDDINGS: EmbeddingRequest,
}

#: Errors listed in a report (the total is always given).
MAX_ERRORS = 1000
#: Stored requests shown in a preview, and the size each is cut to.
PREVIEW_ROWS = 3
PREVIEW_BYTES = 16 * 1024
#: Scans one process runs at once (one per user); more answer ``scan_busy``.
MAX_SCANS = 2
#: The longest one scan may take.
SCAN_TIMEOUT_SECONDS = 120.0
#: Lines read between two yields to the event loop.
_YIELD_EVERY = 500
#: Distinct values a parameter's distribution lists before "other".
_MAX_VALUES = 10
#: Distinct file models listed.
_MAX_MODELS = 50

_ENVELOPE = ("custom_id", "method", "url", "body")
_MAX_CUSTOM_ID = 512
_BOM = b"\xef\xbb\xbf"

#: What to do about a field the gateway would not forward, where there is
#: more to say than "remove it".
_HINTS: dict[str, dict[str, str]] = {
    TARGET_CHAT: {
        "max_completion_tokens": (
            "not forwarded by the gateway and not converted: rename it to max_tokens in the file"
        ),
        "input": "input belongs to Embeddings requests; a Chat request takes messages",
    },
    TARGET_EMBEDDINGS: {
        "messages": "messages belongs to Chat requests; an Embeddings request takes input",
    },
}
#: Chat parameters whose values the console shows and may override.
OVERRIDABLE: dict[str, tuple[str, ...]] = {
    TARGET_CHAT: ("temperature", "max_tokens"),
    TARGET_EMBEDDINGS: (),
}


class ScanBusy(RuntimeError):
    """This process is already scanning as many files as it may (or one of this user's)."""


class ScanTimeout(RuntimeError):
    """A scan ran past :data:`SCAN_TIMEOUT_SECONDS`."""


class SourceUnavailable(RuntimeError):
    """The source file is gone, deleted, expired, not the user's, or not the one previewed."""


class ResolutionChanged(RuntimeError):
    """The model the submission confirmed no longer resolves to the canonical id it showed."""

    def __init__(self, requested: str | None, canonical: str | None) -> None:
        """Record what the model resolves to now."""
        super().__init__(f"{requested!r} now resolves to {canonical!r}")
        self.requested = requested
        self.canonical = canonical


# -- limits on concurrent scans --------------------------------------------------------

_active_users: set[str] = set()


@contextlib.contextmanager
def scan_slot(user_id: str) -> Iterator[None]:
    """Hold one of this process's scan slots for *user_id*, or refuse at once.

    A count, not a semaphore: a scan that would wait is refused, and the
    bookkeeping belongs to no event loop.

    Raises:
        ScanBusy: :data:`MAX_SCANS` scans run already, or one of this user's.
    """
    if user_id in _active_users or len(_active_users) >= MAX_SCANS:
        raise ScanBusy(user_id)
    _active_users.add(user_id)
    try:
        yield
    finally:
        _active_users.discard(user_id)


# -- strict JSON -------------------------------------------------------------------------


class _LineProblem(Exception):
    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _LineProblem(
                "duplicate_key",
                f"An object in this line has the key {key[:64]!r} twice; "
                "keep one (the gateway would otherwise keep the last silently)",
            )
        seen.add(key)
    return dict(pairs)


def _reject_constant(name: str) -> Any:
    raise _LineProblem("invalid_json_line", f"{name} is not valid JSON; use a finite number")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _LineProblem("invalid_json_line", "A number is too large to store; use a finite one")
    return value


def parse_line(raw: bytes, line_no: int) -> dict[str, Any]:
    """One line of a source file as a JSON object, parsed strictly.

    Raises:
        _LineProblem: The line is not a JSON object the console accepts.
    """
    if line_no == 1 and raw.startswith(_BOM):
        raise _LineProblem(
            "utf8_bom", "The file starts with a byte-order mark; save it as UTF-8 without BOM"
        )
    try:
        text = raw.decode("utf-8", errors="strict").rstrip("\r")
    except UnicodeDecodeError:
        raise _LineProblem("invalid_json_line", "The line is not UTF-8 text") from None
    if not text.strip():
        raise _LineProblem("empty_line", "The line is empty; remove it")
    try:
        entry = json.loads(
            text,
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
            parse_float=_finite,
        )
    except json.JSONDecodeError as exc:
        raise _LineProblem(
            "invalid_json_line", f"Not valid JSON at column {exc.colno}: {exc.msg}"
        ) from None
    except RecursionError:
        raise _LineProblem("invalid_json_line", "The line nests too deeply") from None
    except ValueError:
        # Python refuses to convert an integer of more than a few thousand
        # digits (its conversion limit stays on); no request parameter needs one.
        raise _LineProblem(
            "invalid_json_line", "A number in this line has too many digits to read"
        ) from None
    if not isinstance(entry, dict):
        raise _LineProblem("invalid_json_line", "The line is not a JSON object")
    if has_invalid_unicode(entry):
        raise _LineProblem("invalid_json_line", "The line holds a string that is not valid Unicode")
    return entry


# -- the request model's fields --------------------------------------------------------


def _nested_model(annotation: Any) -> tuple[type[BaseModel], bool] | None:
    """The request sub-model a field holds (and whether as a list), or None for free-form JSON."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        for arg in get_args(annotation):
            found = _nested_model(arg)
            if found is not None:
                return found
        return None
    if origin is list:
        args = get_args(annotation)
        if args and inspect.isclass(args[0]) and issubclass(args[0], BaseModel):
            return args[0], True
        return None
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return annotation, False
    return None


def unsupported_fields(value: dict[str, Any], model: type[BaseModel], path: str) -> Iterator[str]:
    """Paths under *value* that *model* does not declare, and so would be dropped.

    The declared fields are the model's own, read at runtime; sub-models
    (``messages[i]``, ``response_format``) are checked a level down, free-form
    JSON (``content``, ``tools``, ``thinking``, a JSON schema) is not.
    """
    fields = {(info.alias or name): info for name, info in model.model_fields.items()}
    for key, item in value.items():
        info = fields.get(key)
        if info is None:
            yield f"{path}.{key[:64]}"
            continue
        nested = _nested_model(info.annotation)
        if nested is None:
            continue
        sub, is_list = nested
        if is_list and isinstance(item, list):
            for index, element in enumerate(item):
                if isinstance(element, dict):
                    yield from unsupported_fields(element, sub, f"{path}.{key}[{index}]")
        elif not is_list and isinstance(item, dict):
            yield from unsupported_fields(item, sub, f"{path}.{key}")


def _validation_problem(exc: ValidationError) -> tuple[str, str]:
    """``(field, message)`` of a body's first schema problem, without its input."""
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    first = errors[0]
    where = "body"
    for part in first.get("loc", ()):
        where += f"[{part}]" if isinstance(part, int) else f".{part}"
    message = str(first["msg"])
    if len(errors) > 1:
        message += f" (and {len(errors) - 1} more)"
    return where, message


def body_shape(body: dict[str, Any]) -> str | None:
    """The type a body's shape alone names, or None when it names none (or both)."""
    has_messages, has_input = "messages" in body, "input" in body
    if has_messages and not has_input:
        return TARGET_CHAT
    if has_input and not has_messages:
        return TARGET_EMBEDDINGS
    return None


# -- the preview -----------------------------------------------------------------------


def _clip(value: Any, limit: int) -> tuple[Any, bool]:
    """*value* with long strings and lists cut; whether anything was cut."""
    if isinstance(value, str):
        return (value[:limit] + "…", True) if len(value) > limit else (value, False)
    if isinstance(value, list):
        cut = len(value) > 20
        items = []
        for element in value[:20]:
            clipped, inner = _clip(element, limit)
            items.append(clipped)
            cut = cut or inner
        if len(value) > 20:
            items.append(f"… {len(value) - 20} more")
        return items, cut
    if isinstance(value, dict):
        cut = False
        result = {}
        for key, element in value.items():
            clipped, inner = _clip(element, limit)
            result[key] = clipped
            cut = cut or inner
        return result, cut
    return value, False


def preview_entry(line_no: int, request: dict[str, Any], filled: list[str]) -> dict[str, Any]:
    """A stored request as the preview shows it: at most :data:`PREVIEW_BYTES`, cuts marked."""
    for limit in (2000, 200, 40):
        clipped, cut = _clip(request, limit)
        if len(json.dumps(clipped, ensure_ascii=False).encode("utf-8")) <= PREVIEW_BYTES:
            break
    else:
        clipped = {key: request[key] for key in ("custom_id", "method", "url")}
        clipped["body"] = "…"
        cut = True
    return {
        "line": line_no,
        "custom_id": request["custom_id"],
        "filled": filled,
        "truncated": cut,
        "request": clipped,
    }


# -- report pieces -----------------------------------------------------------------------


@dataclass
class Overrides:
    """What step 2 sets on every row: ``model`` always; ``temperature``, ``max_tokens`` for Chat."""

    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Only the overrides that are set."""
        return {
            key: value
            for key, value in (
                ("model", self.model),
                ("temperature", self.temperature),
                ("max_tokens", self.max_tokens),
            )
            if value is not None
        }


@dataclass
class _Errors:
    entries: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    lines: set[int] = field(default_factory=set)

    def add(
        self,
        code: str,
        message: str,
        *,
        line: int | None = None,
        custom_id: str | None = None,
        field: str | None = None,
    ) -> None:
        self.total += 1
        if line is not None:
            self.lines.add(line)
        if len(self.entries) < MAX_ERRORS:
            self.entries.append(
                {
                    "line": line,
                    "custom_id": custom_id,
                    "field": field,
                    "code": code,
                    "message": message,
                }
            )


class _Distribution:
    """How often each value of one parameter appears (the first few values, then "other")."""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.other = 0
        self.unset = 0

    def add(self, body: dict[str, Any], key: str) -> None:
        if key not in body:
            self.unset += 1
            return
        label = json.dumps(body[key], ensure_ascii=False)[:40]
        if label in self.values:
            self.values[label] += 1
        elif len(self.values) < _MAX_VALUES:
            self.values[label] = 1
        else:
            self.other += 1

    def view(self) -> dict[str, Any]:
        return {"values": self.values, "other": self.other, "unset": self.unset}


def _custom_id_of(entry: dict[str, Any]) -> str | None:
    custom_id = entry.get("custom_id")
    if isinstance(custom_id, str) and 0 < len(custom_id) <= _MAX_CUSTOM_ID:
        return custom_id
    return None


def _model_problem_code(model: Any, rejected: ItemRejected) -> str:
    if rejected.code == "invalid_request" and isinstance(model, str) and is_auto_model_id(model):
        return "auto_model"
    return rejected.code


class DerivedWriter:
    """Writes the stored requests of a submission to a derived upload, ``uploading`` until adopted."""

    def __init__(
        self, file_store: Any, *, user_id: str, source_file_id: str, filename: str, cap: int
    ) -> None:
        """Bind the writer to the owner and the source it derives from."""
        self._store = file_store
        self._user_id = user_id
        self._source = source_file_id
        self._filename = filename
        self._cap = cap
        self.file_id: str | None = None
        self._pending = bytearray()
        self._seq = 0
        self._digest = hashlib.sha256()
        self.lines = 0

    async def write(self, line: bytes) -> None:
        """Append one stored request (a line, newline included)."""
        if self.file_id is None:
            self.file_id = await self._store.create_derived(
                user_id=self._user_id,
                source_file_id=self._source,
                filename=self._filename,
                max_total_bytes=self._cap,
            )
        self._digest.update(line)
        self.lines += 1
        self._pending += line
        while len(self._pending) >= CHUNK_BYTES:
            await self._flush(bytes(self._pending[:CHUNK_BYTES]))
            del self._pending[:CHUNK_BYTES]

    async def finish(self) -> dict[str, Any]:
        """Store what is left; return ``{"file_id", "sha256", "line_count"}``."""
        if self._pending:
            await self._flush(bytes(self._pending))
            self._pending.clear()
        return {
            "file_id": self.file_id,
            "sha256": self._digest.hexdigest(),
            "line_count": self.lines,
        }

    async def discard(self) -> None:
        """Drop the derived upload (best effort: the collector removes one left behind)."""
        if self.file_id is not None:
            with contextlib.suppress(Exception):
                await self._store.abort_upload(self.file_id)

    async def _flush(self, chunk: bytes) -> None:
        await self._store.append_chunk(
            file_id=self.file_id,
            user_id=self._user_id,
            seq=self._seq,
            data=chunk,
            max_total_bytes=self._cap,
        )
        self._seq += 1


class ConsoleScan:
    """One scan of a source file: identification, and (with a type) validation.

    ``model_info`` maps a canonical model id to its catalog limits
    (``max_output_length``, ``supported_sampling_parameters``), as the
    console's model list shows them.
    """

    def __init__(
        self,
        *,
        store: Any,
        file_store: Any,
        ctx: AdmissionContext,
        config: BatchConfig,
        source: dict[str, Any],
        overrides: Overrides,
        endpoint_choice: str | None = None,
        model_info: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Prepare a scan of *source* (a ready upload row)."""
        self._store = store
        self._file_store = file_store
        self._ctx = ctx
        self._config = config
        self._source = source
        self._overrides = overrides
        self._choice = endpoint_choice
        self._model_info = model_info or {}
        self._errors = _Errors()
        # Pass one.
        self._rows = 0
        self._type_rows: dict[str, list[tuple[int, str | None]]] = {
            TARGET_CHAT: [],
            TARGET_EMBEDDINGS: [],
        }
        self._type_counts = {TARGET_CHAT: 0, TARGET_EMBEDDINGS: 0}
        self._undetermined = 0
        self._explicit_url = 0
        self._missing_url = 0
        self._models: dict[str, int] = {}
        self._models_other = 0
        self._model_unset = 0
        self._params = {key: _Distribution() for key in ("temperature", "max_tokens")}
        self._other_params: dict[str, int] = {}
        self._stopped = False
        self._rejected: set[str] = set()
        # The outcome.
        self.target: str | None = None
        self.type_source: str | None = None
        self.needs_type_choice = False
        self.canonical: str | None = None
        self.requested: str | None = None
        self.preview: list[dict[str, Any]] = []
        self.stores_copy = False
        self.request_count = 0
        #: Bytes of the stored requests, as written (the derived file's size).
        self.stored_bytes = 0
        self._model_view: list[dict[str, Any]] = []

    # -- running -------------------------------------------------------------------------

    async def run(self, *, validate: bool, writer: DerivedWriter | None = None) -> None:
        """Identify the file and, when ``validate`` and the type is known, validate it.

        With a ``writer``, the stored requests are written as they are checked
        whenever they differ from the source; the caller adopts or discards it.

        Raises:
            ScanTimeout: The scan took longer than :data:`SCAN_TIMEOUT_SECONDS`.
            SourceUnavailable: The source's bytes could not be leased.
        """
        try:
            lease = await self._store.take_read_lease(
                file_id=self._source["id"], ttl_seconds=READ_LEASE_SECONDS
            )
        except LookupError:
            raise SourceUnavailable(self._source["id"]) from None
        keeper = LeaseKeeper(self._store, lease)
        try:
            async with asyncio.timeout(SCAN_TIMEOUT_SECONDS):
                await self._identify()
                self._decide_type()
                if self.target is not None:
                    await self._describe_models()
                if validate and self.target is not None and not self._stopped:
                    await self._validate(writer)
                if keeper.lost:
                    raise SourceUnavailable(self._source["id"])
        except TimeoutError:
            raise ScanTimeout(self._source["id"]) from None
        finally:
            await keeper.close()

    async def _lines(self) -> Any:
        """``(line number, bytes)`` of the source, stopping at a line over the limit."""
        try:
            async for line_no, raw in iter_lines(
                self._file_store, self._source["id"], max_line_bytes=self._config.max_line_bytes
            ):
                if line_no % _YIELD_EVERY == 0:
                    await asyncio.sleep(0)
                yield line_no, raw
        except LineTooLong as exc:
            if not self._stopped:
                self._errors.add(
                    "line_too_long",
                    f"The line is longer than {self._config.max_line_bytes // 1024} KiB; "
                    "the rest of the file was not checked",
                    line=exc.entry.get("line"),
                )
            self._stopped = True

    # -- pass one: identification --------------------------------------------------------

    async def _identify(self) -> None:
        if int(self._source.get("bytes") or 0) > self._config.max_body_bytes:
            self._errors.add(
                "file_too_large",
                f"The file is larger than {self._config.max_body_bytes // (1024 * 1024)} MiB",
            )
            self._stopped = True
            return
        seen: dict[bytes, int] = {}
        async for line_no, raw in self._lines():
            self._rows += 1
            if self._rows > self._config.max_requests:
                self._errors.add(
                    "too_many_requests",
                    f"The file holds more than {self._config.max_requests} requests; split it",
                    line=line_no,
                )
                self._stopped = True
                break
            self._identify_row(line_no, raw, seen)

    def _identify_row(self, line_no: int, raw: bytes, seen: dict[bytes, int]) -> None:
        errors = self._errors
        try:
            entry = parse_line(raw, line_no)
        except _LineProblem as problem:
            errors.add(problem.code, problem.message, line=line_no, field=problem.field)
            return
        custom_id = _custom_id_of(entry)
        failed = False
        if custom_id is None or "\x00" in custom_id:
            errors.add(
                "invalid_custom_id",
                f"custom_id must be a string of 1 to {_MAX_CUSTOM_ID} characters",
                line=line_no,
                field="custom_id",
            )
            failed = True
            custom_id = None
        else:
            digest = hashlib.sha1(custom_id.encode("utf-8")).digest()
            first = seen.get(digest)
            if first is not None:
                errors.add(
                    "duplicate_custom_id",
                    f"custom_id is the same as on line {first}; every request needs its own",
                    line=line_no,
                    custom_id=custom_id,
                    field="custom_id",
                )
                failed = True
            else:
                seen[digest] = line_no
        for key in entry:
            if key not in _ENVELOPE:
                errors.add(
                    "unsupported_parameter",
                    "Not part of a batch request line ({custom_id, method, url, body}); remove it",
                    line=line_no,
                    custom_id=custom_id,
                    field=key[:64],
                )
                failed = True
        if "method" in entry and entry["method"] != "POST":
            errors.add(
                "invalid_method",
                "method must be POST",
                line=line_no,
                custom_id=custom_id,
                field="method",
            )
            failed = True
        explicit: str | None = None
        if "url" in entry:
            self._explicit_url += 1
            url = entry["url"]
            explicit = CONSOLE_TARGETS.get(url) if isinstance(url, str) else None
            if explicit is None:
                errors.add(
                    "unsupported_endpoint",
                    (
                        "This request type cannot be created in the console yet; "
                        "submit it through /v1/batches"
                        if isinstance(url, str) and url in TARGET_URLS
                        else "url must be /v1/chat/completions or /v1/embeddings"
                    ),
                    line=line_no,
                    custom_id=custom_id,
                    field="url",
                )
                failed = True
        else:
            self._missing_url += 1
        body = entry.get("body")
        if not isinstance(body, dict):
            errors.add(
                "invalid_request",
                "body must be a JSON object",
                line=line_no,
                custom_id=custom_id,
                field="body",
            )
            return
        shape = body_shape(body)
        if explicit is not None and shape is not None and shape != explicit:
            errors.add(
                "mismatched_endpoint",
                f"url is {TARGET_URL[explicit]} but the body is shaped like a "
                f"{TARGET_URL[shape]} request; fix the url or the body",
                line=line_no,
                custom_id=custom_id,
                field="url",
            )
            return
        self._tally(body)
        if failed and explicit is None and "url" in entry:
            return
        row_type = explicit or shape
        if row_type is None:
            self._undetermined += 1
            return
        self._type_counts[row_type] += 1
        refs = self._type_rows[row_type]
        if len(refs) < MAX_ERRORS:
            refs.append((line_no, custom_id))

    def _tally(self, body: dict[str, Any]) -> None:
        model = body.get("model")
        if "model" not in body:
            self._model_unset += 1
        else:
            name = model if isinstance(model, str) else json.dumps(model)[:40]
            if name in self._models:
                self._models[name] += 1
            elif len(self._models) < _MAX_MODELS:
                self._models[name] = 1
            else:
                self._models_other += 1
        for key, distribution in self._params.items():
            distribution.add(body, key)
        for key in body:
            if key not in ("model", "messages", "input", "temperature", "max_tokens"):
                name = key[:64]
                self._other_params[name] = self._other_params.get(name, 0) + 1

    def _decide_type(self) -> None:
        present = [target for target, count in self._type_counts.items() if count]
        if len(present) > 1:
            major = max(present, key=lambda target: self._type_counts[target])
            counts = ", ".join(
                f"{self._type_counts[target]} {TARGET_URL[target]}" for target in present
            )
            for target in present:
                if target == major:
                    continue
                for line_no, custom_id in self._type_rows[target]:
                    self._errors.add(
                        "mismatched_endpoint",
                        f"The file mixes request types ({counts}); a batch runs one type: "
                        "split it into one file per type",
                        line=line_no,
                        custom_id=custom_id,
                        field="url",
                    )
                self._errors.total += self._type_counts[target] - len(self._type_rows[target])
            return
        choice = CONSOLE_TARGETS.get(self._choice or "")
        if self._choice is not None and choice is None:
            self._errors.add(
                "unsupported_endpoint",
                "The request type must be /v1/chat/completions or /v1/embeddings",
                field="endpoint",
            )
            return
        if present:
            self.target = present[0]
            self.type_source = "url" if self._explicit_url else "structure"
            if choice is not None and choice != self.target:
                self.target = None
                self._errors.add(
                    "mismatched_endpoint",
                    "The file's requests are of another type than the one chosen; "
                    "a choice only fills in a type the file does not state",
                    field="endpoint",
                )
            return
        if self._undetermined == 0:
            return
        if choice is None:
            self.needs_type_choice = True
            return
        self.target = choice
        self.type_source = "choice"

    async def _describe_models(self) -> None:
        for name, rows in self._models.items():
            entry: dict[str, Any] = {"name": name, "rows": rows, "available": False}
            try:
                entry["canonical"] = await resolve_model(self.target or "", name, self._ctx)
                entry["available"] = True
            except ItemRejected:
                entry["canonical"] = None
            self._model_view.append(entry)

    # -- pass two: validation ------------------------------------------------------------

    def _check_overrides(self) -> bool:
        """Check the parameter overrides; an invalid one is reported once and not applied."""
        target, overrides = self.target, self._overrides
        good = True
        if target == TARGET_EMBEDDINGS:
            for key in ("temperature", "max_tokens"):
                if getattr(overrides, key) is not None:
                    self._errors.add(
                        "invalid_request",
                        f"Embeddings requests take no {key}",
                        field=f"overrides.{key}",
                    )
                    self._rejected.add(key)
                    good = False
        temperature = overrides.temperature
        if temperature is not None and (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature)
            or not 0 <= temperature <= 2
        ):
            self._errors.add(
                "invalid_request", "temperature must be from 0 to 2", field="overrides.temperature"
            )
            self._rejected.add("temperature")
            good = False
        max_tokens = overrides.max_tokens
        if max_tokens is not None and (
            isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1
        ):
            self._errors.add(
                "invalid_request",
                "max_tokens must be a whole number of at least 1",
                field="overrides.max_tokens",
            )
            self._rejected.add("max_tokens")
            good = False
        return good

    async def _resolve_override(self) -> bool:
        model = self._overrides.model
        if model is None:
            return True
        self.requested = model
        try:
            self.canonical = await resolve_model(self.target or "", model, self._ctx)
        except ItemRejected as rejected:
            self._errors.add(
                _model_problem_code(model, rejected), rejected.message, field="overrides.model"
            )
            return False
        return True

    def _check_limits(self) -> None:
        info = self._model_info.get(self.canonical or "")
        if not info or self.target != TARGET_CHAT:
            return
        # The catalog's list is the model's declared parameters (the
        # intersection over its routes); the adapters drop any other. An empty
        # list declares none, so it is not "unknown".
        supported = info.get("supported_sampling_parameters")
        if supported is not None:
            for key in ("temperature", "max_tokens"):
                if getattr(self._overrides, key) is not None and key not in supported:
                    self._errors.add(
                        "unsupported_parameter",
                        f"This model does not take {key}; keep the file's values",
                        field=f"overrides.{key}",
                    )
        limit = int(info.get("max_output_length") or 0)
        if self._overrides.max_tokens is not None and 0 < limit < self._overrides.max_tokens:
            self._errors.add(
                "invalid_request",
                f"max_tokens may be at most {limit} for this model",
                field="overrides.max_tokens",
            )

    async def _validate(self, writer: DerivedWriter | None) -> None:
        overrides_ok = self._check_overrides() and await self._resolve_override()
        if not overrides_ok:
            writer = None
        target = self.target or ""
        url = TARGET_URL[target]
        request_model = _REQUEST_MODELS[target]
        hints = _HINTS.get(target, {})
        changes = {
            key: value
            for key, value in self._overrides.as_dict().items()
            if key not in self._rejected
        }
        pass_one_errors = self._errors.total
        file_canonical: str | None = None
        names: dict[str, str] = {}
        rewrite = bool(changes) or self._missing_url > 0
        if not rewrite and self._overrides.model is None:
            # Pinning an alias to its canonical id also changes what is stored.
            rewrite = any(entry["name"] != entry.get("canonical") for entry in self._model_view)
        self.stores_copy = rewrite
        write = writer is not None and rewrite and pass_one_errors == 0
        async for line_no, raw in self._lines():
            if line_no in self._errors.lines:
                continue
            try:
                entry = parse_line(raw, line_no)
            except _LineProblem:
                continue
            custom_id = _custom_id_of(entry) or ""
            body = dict(entry["body"])
            row_ok = True
            for path in unsupported_fields(body, request_model, "body"):
                key = path.split(".", 2)[1].split("[")[0] if path.count(".") == 1 else ""
                self._errors.add(
                    "unsupported_parameter",
                    hints.get(key, "Not forwarded by the gateway; remove it from the file"),
                    line=line_no,
                    custom_id=custom_id,
                    field=path,
                )
                row_ok = False
            for key in ("temperature", "max_tokens"):
                if key in changes:
                    body[key] = changes[key]
            if self._overrides.model is not None:
                # Unresolved (already reported once): checked under its own name.
                body["model"] = self.canonical or self._overrides.model
            elif "model" not in body:
                self._errors.add(
                    "missing_model",
                    "body.model is missing; add it, or choose one model for every row in step 2",
                    line=line_no,
                    custom_id=custom_id,
                    field="body.model",
                )
                continue
            try:
                parsed = request_model.model_validate(body, strict=True)
            except ValidationError as exc:
                where, message = _validation_problem(exc)
                self._errors.add(
                    "invalid_request", message, line=line_no, custom_id=custom_id, field=where
                )
                continue
            if getattr(parsed, "stream", False):
                self._errors.add(
                    "invalid_request",
                    "stream must be false or left out in a batch",
                    line=line_no,
                    custom_id=custom_id,
                    field="body.stream",
                )
                continue
            if not row_ok:
                continue
            if self._overrides.model is None:
                name = body["model"]
                canonical = names.get(name)
                if canonical is None:
                    try:
                        canonical = await resolve_model(target, name, self._ctx)
                    except ItemRejected as rejected:
                        self._errors.add(
                            _model_problem_code(name, rejected),
                            rejected.message,
                            line=line_no,
                            custom_id=custom_id,
                            field="body.model",
                        )
                        continue
                    names[name] = canonical
                if file_canonical is None:
                    file_canonical = canonical
                elif canonical != file_canonical:
                    self._errors.add(
                        "mismatched_model",
                        f"Every row must use one model; this row's resolves to {canonical!r}, "
                        f"earlier rows' to {file_canonical!r}. Choose one model in step 2",
                        line=line_no,
                        custom_id=custom_id,
                        field="body.model",
                    )
                    continue
                body["model"] = canonical
            stored = {"custom_id": entry["custom_id"], "method": "POST", "url": url, "body": body}
            encoded = (json.dumps(stored, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            if len(encoded) > self._config.max_line_bytes:
                self._errors.add(
                    "line_too_long",
                    f"With the overrides applied the request is longer than "
                    f"{self._config.max_line_bytes // 1024} KiB",
                    line=line_no,
                    custom_id=custom_id,
                )
                continue
            self.request_count += 1
            self.stored_bytes += len(encoded)
            if len(self.preview) < PREVIEW_ROWS:
                filled = []
                if "url" not in entry:
                    filled.append("url")
                if "method" not in entry:
                    filled.append("method")
                if entry["body"].get("model") != body["model"]:
                    filled.append("body.model")
                filled.extend(f"body.{key}" for key in changes if key != "model")
                self.preview.append(preview_entry(line_no, stored, filled))
            if (
                write
                and self._errors.total == 0
                and writer is not None
                and self.stored_bytes <= self._config.max_body_bytes
            ):
                await writer.write(encoded)
        if self.stores_copy and self.stored_bytes > self._config.max_body_bytes:
            # Each line may be within its limit and the source within the file
            # limit, yet the filled-in url, pinned model and overrides grow the
            # stored file past it.
            limit_mib = self._config.max_body_bytes // (1024 * 1024)
            self._errors.add(
                "file_too_large",
                f"With the filled-in fields and overrides the stored requests come to "
                f"{self.stored_bytes / (1024 * 1024):.1f} MiB, over the {limit_mib} MiB "
                "limit; split the file",
            )
        if self._overrides.model is None:
            self.canonical = file_canonical
            self.requested = file_canonical
        if self.canonical is not None:
            self._check_limits()
        if self._rows == 0 and not self._errors.total:
            self._errors.add("invalid_request", "The file holds no requests")

    # -- the report ----------------------------------------------------------------------

    @property
    def valid(self) -> bool:
        """Whether the file may be submitted as configured (validated, with no error)."""
        return (
            self._errors.total == 0
            and self.target is not None
            and self.canonical is not None
            and self.request_count > 0
        )

    @property
    def errors(self) -> list[dict[str, Any]]:
        """The listed errors, by line (file-level problems first)."""
        return sorted(self._errors.entries, key=lambda entry: entry["line"] or 0)

    def report(self) -> dict[str, Any]:
        """The answer of a preflight (and the body of a refused submission)."""
        target = self.target
        params = {}
        if target is not None:
            params = {key: self._params[key].view() for key in OVERRIDABLE[target]}
        return {
            "source": {
                "id": self._source["id"],
                "filename": self._source.get("filename"),
                "sha256": self._source.get("sha256"),
                "bytes": int(self._source.get("bytes") or 0),
                "lines": self._rows,
            },
            "detected": {
                "type": target,
                "endpoint": TARGET_URL.get(target or ""),
                "source": self.type_source,
                "needs_type_choice": self.needs_type_choice,
                "rows": {
                    "chat": self._type_counts[TARGET_CHAT],
                    "embeddings": self._type_counts[TARGET_EMBEDDINGS],
                    "undetermined": self._undetermined,
                },
                "url_missing_rows": self._missing_url,
                "models": self._model_view,
                "models_other_rows": self._models_other,
                "model_unset_rows": self._model_unset,
            },
            "params": params,
            "other_params": dict(sorted(self._other_params.items())),
            "model_resolution": (
                {"requested": self.requested, "canonical": self.canonical}
                if self.canonical is not None
                else None
            ),
            "valid": self.valid,
            "request_count": self.request_count,
            "stores_copy": self.stores_copy,
            "stored_bytes": self.stored_bytes,
            "error_count": self._errors.total,
            "errors_truncated": self._errors.total > len(self._errors.entries),
            "errors": self.errors,
            "preview": self.preview,
        }
