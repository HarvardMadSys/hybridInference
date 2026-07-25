# API Headers Reference

Headers you can send with requests to the FreeInference API.

## Authentication

### `Authorization`

Authenticate requests using your API key.

```bash
Authorization: Bearer hyi-your-api-key
```

### `X-API-Key`

Alternative authentication header. Accepts the same API key value without the `Bearer` prefix.

```bash
X-API-Key: hyi-your-api-key
```

## Request Behavior

### `X-Reasoning-Passthrough`

Controls how `reasoning_content` is handled in responses from `/v1/chat/completions`.

| Value | Behavior |
|-------|----------|
| (missing or any other value) | **Passthrough mode** — `reasoning_content` is included in the response |
| `false`, `0`, `no` (case-insensitive) | **Strict OpenAI mode** — `reasoning_content` is stripped from the response |

```bash
X-Reasoning-Passthrough: false
```

### `X-Session-ID`

Attach a stable session identifier for request correlation. This value is logged and can be used to trace a session across multiple requests.

```bash
X-Session-ID: 20260503-120000-abc123
```

### `X-Probe`

Mark a request as a synthetic health probe. When set to `synthetic` (case-insensitive), the request does not affect metrics or generate verbose logs.

```bash
X-Probe: synthetic
```

### `X-Route-Pin`

Force routing to a specific provider backend. **Admin-only** — silently ignored for non-admin users.

```bash
X-Route-Pin: provider-name
```

## Anthropic API

### `Anthropic-Version`

Optional — used only for Anthropic client detection (e.g., to switch the response shape returned by `/v1/models`). Not enforced on `/v1/messages` or `/anthropic/v1/messages`, and not forwarded upstream by this gateway.

```bash
Anthropic-Version: 2023-06-01
```

### `anthropic-beta`

Enable Anthropic beta features. This header is forwarded directly to the upstream Anthropic API.

```bash
anthropic-beta: feature-name
```

## Tracing

### `X-Request-ID`

Attach a custom request identifier. If not provided, one is auto-generated. The value is echoed back in the response.

```bash
X-Request-ID: my-custom-request-id
```

## Qdrant Proxy

### `api-key`

Authentication for the Qdrant proxy endpoint at `/v1/qdrant/`. Mapped to `Authorization: Bearer <value>` internally.

```bash
api-key: your-qdrant-key
```

## Proxy Headers

The following headers are used for client IP resolution when the server is behind a reverse proxy (requires `TRUST_PROXY_HEADERS=1`):

Resolution order (first match wins):

| Header | Description |
|--------|-------------|
| `CF-Connecting-IPv6` | The visitor's real IPv6 address. Sent by Cloudflare only when Pseudo IPv4 is set to "Overwrite headers", where `CF-Connecting-IP` instead carries a synthetic Class E IPv4. Honored only when `CF-Connecting-IP` corroborates it by holding that synthetic — otherwise ignored, since the header is absent (not cleared) on ordinary requests and so is caller-supplied |
| `CF-Connecting-IP` | Client IP as set by Cloudflare. Preferred over the forwarding headers — Cloudflare always overwrites this one, while it only *appends* to `X-Forwarded-For`. Requires `TRUST_CLOUDFLARE_HEADERS=1`, which must be enabled only when Cloudflare is the immediate proxy |
| `X-Forwarded-For` | Fallback for non-Cloudflare proxies (the first **routable** entry is used; leading private/loopback/ULA hops an intermediary inserted are skipped) |
| `X-Real-IP` | Fallback client IP header |

If none match — or `TRUST_PROXY_HEADERS` is not `1` — the socket peer address is used.

IPv6 client addresses are logged in full. For per-client rate limits and sticky
routing they are grouped by `/64`, since a single client is typically delegated
an entire prefix and its addresses may rotate. IPv4 addresses — including
IPv4-mapped literals such as `::ffff:192.0.2.1` — are grouped per address.

## Standard Headers

| Header | Description |
|--------|-------------|
| `Content-Type` | Request content type (e.g., `application/json`) |
| `User-Agent` | Client identification; affects Anthropic client detection |
| `Host` | Logged for debugging |
