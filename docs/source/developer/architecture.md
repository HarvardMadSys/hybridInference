# Architecture Overview

HybridInference is designed as a modular, high-performance inference gateway.

## System Architecture

```
┌─────────────┐
│   Clients   │
└──────┬──────┘
       │
       ▼
┌─────────────────┐
│  FastAPI Gateway│
│   (serving/)    │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
┌────────┐ ┌─────────┐
│Routing │ │Adapters │
│Manager │ │         │
└────┬───┘ └────┬────┘
     │          │
     ▼          ▼
┌───────────────────────┐
│    LLM Providers      │
│ ┌───────────────────┐ │
│ │ Local vLLM        │ │
│ │ OpenAI API        │ │
│ │ Gemini API        │ │
│ │ Claude Sub (OAuth)│ │
│ │ Codex Sub (OAuth) │ │
│ └───────────────────┘ │
└───────────────────────┘
```

## Network Layer

External traffic passes through three layers before reaching the application logic:

```
Client ──▶ Cloudflare (CDN + DDoS) ──▶ Nginx (:443) ──▶ FastAPI (:8080)
                                                    └──▶ Frontend (:3001)
```

- **Cloudflare**: Edge CDN, DDoS protection, SSL termination (Full strict mode).
- **Nginx**: Origin TLS, path-based routing, body size limits, WebSocket upgrade.
- **FastAPI**: API authentication, model routing, rate limiting, observability.

The network layer handles external connectivity and request delivery. The sections below describe the internal inference pipeline that runs inside FastAPI.

## Core Components

### Serving Layer (`serving/`)

The serving layer provides the FastAPI-based gateway:

- **Gateway**: HTTP API endpoints for inference requests
- **Adapters**: Provider-specific API adapters
- **Observability**: Logging, metrics, and tracing
- **Storage**: PostgreSQL integration for request/response logging

### Routing Layer (`routing/`)

Intelligent routing and load balancing:

- **Manager**: Routes requests to optimal providers
- **Strategies**: Different routing algorithms (round-robin, cost-based, latency-based)
- **Health Checks**: Monitor provider availability and performance

### Configuration (`config/`)

Centralized configuration management:

- Model configurations
- Provider settings
- Routing policies
- Feature flags

### Infrastructure (`infrastructure/`)

Deployment and observability:

- Docker Compose service definitions and Dockerfiles
- Alertmanager and alert logger

### Subscription Adapters

Some providers are accessed through **OAuth subscription accounts** rather than static API keys. These adapters have a layered credential architecture:

```
┌──────────────────────────────────────────────────────────────┐
│               Subscription Adapter (e.g. claude_sub)         │
│   ┌────────────────────┐  ┌────────────────────────────┐    │
│   │   AccountPool       │  │  CredentialProvider         │    │
│   │   (codex_token.py)  │  │  (claude_token.py)          │    │
│   │                     │  │                             │    │
│   │  Health-aware       │  │  OAuth token lifecycle:     │    │
│   │  round-robin        │  │  - refresh before expiry    │    │
│   │  rotation           │  │  - invalid_grant detection  │    │
│   │                     │  │  - state persistence (JSON) │    │
│   │  deactivate/activate│  │  - transition_state()       │    │
│   └────────────────────┘  └────────────────────────────┘    │
│                                                              │
│   Account states: active → cooldown → revoked/disabled       │
│   Fallback: optional paid API key when all accounts down     │
└──────────────────────────────────────────────────────────────┘
```

Two subscription adapters currently exist:

| Adapter | Provider | Protocol | Credential File |
|---|---|---|---|
| `ClaudeSubscriptionAdapter` | Anthropic (Claude Code OAuth) | Messages API | `var/data/claude_accounts.json` |
| `CodexSubscriptionAdapter` | OpenAI (Codex CLI OAuth) | Responses API | `var/data/codex_accounts.json` |

Both share `AccountPool` (from `codex_token.py`) for health-aware rotation. Each has its own `CredentialProvider` for provider-specific OAuth endpoints.

For Claude, the account pool is shared process-wide between:
- `ClaudeSubscriptionAdapter` (OpenAI-compatible `/v1/chat/completions`)
- `anthropic_proxy.py` (`POST /anthropic/v1/messages`)

This shared singleton keeps cooldown and revoke state consistent across both surfaces.

### Northbound API Surfaces

The gateway currently exposes more than one client-facing protocol surface:

| Surface | Endpoint | Typical clients | Notes |
|---|---|---|---|
| OpenAI-compatible Chat Completions | `POST /v1/chat/completions` | SDKs, OpenAI-compatible tools | Primary public surface |
| Anthropic-compatible Messages | `POST /anthropic/v1/messages` | Claude Code CLI | Only models routed through `provider: claude_sub` are eligible |

The Anthropic surface is an **identity surface translator**: the client-facing and upstream protocols are both Anthropic Messages API, so the route mainly performs auth, rate limiting, model resolution, credential injection, and usage logging.

For design details, see:
- repository design doc `docs/claude-account-lifecycle.md` — account state machine, error handling, data model
- repository design doc `docs/subscription-adapter-architecture.md` — long-term multi-provider architecture

## Key Design Principles

1. **Modularity**: Clear separation between serving, routing, and provider layers
2. **Extensibility**: Easy to add new providers and routing strategies
3. **Observability**: Comprehensive logging and metrics at every layer
4. **Performance**: Optimized for low-latency, high-throughput inference
5. **Reliability**: Health checks, retries, and fallback mechanisms

## Data Flow

1. Client sends inference request to Gateway
2. Gateway validates and preprocesses request
3. Routing Manager selects optimal provider
4. Adapter translates request to provider-specific format
5. Provider processes inference
6. Response is logged to PostgreSQL
7. Response is returned to client
