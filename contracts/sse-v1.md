# Streaming contract v1

OpenAI-compatible streams use `text/event-stream`, blank-line event
termination, single-line `data:` JSON frames, and a final `data: [DONE]`
event. Anthropic-compatible streams preserve Anthropic `event:`/`data:`
framing and terminate with `message_stop`. Streaming responses set
`Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no`.

Conformance evidence is split intentionally because OpenAPI cannot describe
stream lifecycles:

- `tests/servers/test_contract_openai_api.py` covers the OpenAI first frame,
  JSON chunks, terminal frame, content type, and proxy/cache headers.
- `tests/servers/test_anthropic_messages_router.py` covers native passthrough
  and translated Anthropic start/stop framing plus proxy/cache headers.
- `tests/unit/servers/test_completions_stream.py` covers provider failures,
  malformed/empty streams, idle timeout keepalives, cancellation, disconnect,
  and abnormal termination logging.
- `tests/unit/servers/test_sse.py` and
  `tests/unit/servers/test_sse_crlf_split.py`, plus the independent frontend
  fixture tests, cover arbitrary byte and UTF-8 boundaries, LF/CRLF/CR
  delimiters, empty events, and `[DONE]`.
