# Request Log Null-Byte Sanitization

**Date:** 2026-05-15
**Status:** Approved (design)
**Author:** Juncheng Yang (with Kilo)

## Context

Production emitted `tracked_task_failure_rate` alerts for the `request_log`
background task. Investigation on `jason@internal.freeinference.org` showed the
request-log insert into `api_logs` was failing with PostgreSQL
`UntranslatableCharacterError`:

```text
unsupported Unicode escape sequence
DETAIL:  \u0000 cannot be converted to text.
```

The failures came from the asynchronous request-log write path, not from the
main HTTP response path. User requests could still return success while the
database log row was dropped.

## Goals

- Prevent request-log inserts from failing when payloads contain null bytes.
- Preserve existing request logging behavior aside from removing the invalid
  null-byte characters.
- Keep the fix minimal and localized to the storage/request-log path.
- Add regression coverage proving the bad payload no longer breaks logging.

## Non-Goals

- Broad content normalization beyond null-byte removal.
- Changes to alert thresholds or tracked-task behavior.
- Changes to non-request-log storage code unless required to keep the two
  PostgreSQL log implementations consistent.

## Root Cause

Some logged request fields can contain embedded `\x00` characters. PostgreSQL
rejects null bytes in both `TEXT` values and JSON strings stored through JSONB
casts. The request-log code currently serializes prompt/response/metadata/tools
payloads directly and forwards them to the insert statement unchanged.

## Recommended Approach

Add a small recursive sanitizer in the storage utility layer that strips null
bytes from all strings. Apply it immediately before request-log serialization in
both PostgreSQL log implementations:

- `apps/backend/serving/storage/database.py`
- `apps/backend/serving/storage/postgres_log.py`

This keeps the behavior change narrowly scoped to the failing persistence path
while covering both top-level text columns and nested JSON content.

## Alternatives Considered

### 1. Sanitize only the top-level `TEXT` columns

Why not:

- it would miss nested JSON fields in `metadata`, `request_payload`, `tools`,
  or structured `response` payloads
- the production error came from the insert statement as a whole, so a partial
  fix would leave hidden failure paths

### 2. Retry failed inserts with a degraded payload

Why not:

- more moving parts in a production bugfix
- harder to reason about and test
- unnecessary once the root cause is understood

### 3. Catch and suppress the insert error

Why not:

- it would hide the root cause rather than fixing it
- request rows would still be lost

## Design

### 1. Sanitizer behavior

Add a utility function in `apps/backend/serving/storage/utils.py` that:

- removes `\x00` from strings
- traverses dicts, lists, and tuples recursively
- leaves non-string scalar values unchanged

This function should compose with the existing `json_safe()` helper. The two
concerns are different:

- `json_safe()` handles non-finite floats for JSON serialization
- the new sanitizer handles PostgreSQL-incompatible null bytes in strings

### 2. Request-log serialization changes

Update both PostgreSQL request-log implementations so that all serialized values
used by the `api_logs` insert are sanitized before conversion to SQL
parameters.

Affected request-log content includes:

- `prompt`
- `response`
- `request_payload`
- `metadata`
- `params.tools`
- `error`

The change should preserve the existing privacy-mode behavior where prompt and
response storage can be disabled.

### 3. Test coverage

Add focused unit coverage for the storage utility/request-log serialization path
that reproduces the production condition with embedded null bytes.

The regression test should prove:

- null bytes are removed from serialized request-log inputs
- nested JSON content is sanitized recursively
- the request-log method still reaches the DB execute call successfully with the
  cleaned payload

## Risks And Mitigations

### Risk: the sanitizer changes stored content

Mitigation:

- only null bytes are stripped
- all other content remains unchanged
- the removed character is invalid for PostgreSQL storage anyway

### Risk: only one PostgreSQL implementation is fixed

Mitigation:

- update both `DatabaseLogger` and `PostgresLogStore`
- keep the sanitization logic in shared storage utilities

## Validation

Minimum validation:

- a focused unit test for null-byte request-log payloads fails before the fix
- the same test passes after the fix
- existing tracked-task/request-log tests still pass

Practical command set:

- `uv run pytest tests/unit/storage/test_request_log_sanitization.py -v`
- `uv run pytest tests/unit/serving/test_completions_tracked_tasks.py -v`
