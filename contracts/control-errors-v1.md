# Stable control error contract v1

Every non-success response from an operation in
`openapi/control-v1.openapi.json` uses this minimum envelope:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed",
    "details": {}
  },
  "request_id": "request-correlation-id"
}
```

Clients branch on `error.code`, never on the human-readable `message`.
`details` is always an object but its fields are error-specific and optional.
Unknown future codes must be handled as an operation failure without treating
the message as a protocol discriminator. The checked OpenAPI snapshot carries
the current code vocabulary on each operation as `x-control-error-codes` and a
`default` response that references `ControlErrorResponse`.

During the compatibility window responses may also contain legacy top-level
`error_code`, `message`, `timestamp`, and detail fields. They are not part of
the v1 minimum envelope. OpenAI and Anthropic inference endpoints retain their
protocol-native error formats and are not covered by this contract.
