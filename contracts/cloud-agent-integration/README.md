# Optional Cloud Agent integration contract

Status: **proposed P0 contract; runtime implementation pending** (2026-09-09).

HybridInference owns this provider contract. Cloud Agent consumes a pinned copy
with its own HTTP clients; it does not import gateway implementation packages.
FreeInference supplies deployment policy and consumes gateway/console images
through its upstream lock. The implementation sequence and inspected source
commits are in the [stepwise plan](../../docs/agents/plans/2026-09-09-cloud-agent-integration-stepwise.zh.md).

This directory contains the [OpenAPI document](v1/openapi.yaml) and
[positive/negative schema fixtures](v1/fixtures.json). The document specifies
future behavior, including a new identity variant on existing paths. It does
not describe endpoints currently available from an unmodified gateway.

## Versions and compatibility

The proposed bundle uses binding/access/catalog/grants major 1 and **identity
major 2**. Identity major 2 is the installation-scoped, profile-free protocol;
the consumer's existing identity v1 contract remains the explicit legacy
variant. The optional deletion protocol is `subject_lifecycle.v1`.

Discovery advertises only versions the deployed provider actually implements.
The client verifies a supported version of each required family before redeem
or activation and persists that selection with its binding. An unsupported
major is an error, never a reason to use a legacy dispatch token or profileful
identity variant. A provider may advertise multiple positive major versions;
the client selects a version it supports rather than rejecting an advertisement
that also includes a newer major. Missing optional lifecycle support does not
disable core integration. Lifecycle scope, version and endpoint are required
together only when that capability is approved and selected.

For identity, the gateway console's discovered authorize URL carries the
installation client and `contract_version=2` through the flow. The existing
`POST /v1/identity/code` and `/token` paths require that explicit version for
installation clients. The old `cloud-agent` client remains confined to an
explicitly imported legacy installation; this draft does not change its current
runtime behavior. An unknown client cannot opt into legacy behavior by omitting
the version. JWT headers still specify the existing signing algorithm/key ID.

Field removal, changed authorization semantics, broader permissions or new
profile fields require a new major. Strict allowlist messages cannot grow new
fields under a supposedly compatible minor version. Examples are fixtures for
this proposed contract, not captured production traffic or compatibility proof.

## Authority and trust

- The gateway owns accounts, canonical subject IDs, global policy, quota,
  installation consent and grants. Agent owns sessions, tasks, connections,
  runner leases, fencing and object-level ownership.
- One Agent database belongs to one persisted `(gateway_instance_id, issuer)`.
  Disconnect retains that namespace and all business owners. An existing
  database cannot be attached to another namespace by reusing matching `sub`
  strings. A gateway clone that becomes a new identity domain gets a new ID.
- Pairing authorizes a service installation. User consent authorizes a subject
  within that installation. Neither replaces the other.
- Service credential authentication derives installation identity; a body
  cannot choose which installation owns a lookup, grant or usage query.
- The scopes are `access`, `catalog`, `grants` and optional
  `subject_lifecycle`. Application permissions `agent.use` and `agent.admin`
  are gateway decisions, not role names or Agent installation-setup rights.

Discovery is metadata, not authentication. Agent starts with an origin entered
by its deployment administrator, persists the canonical issuer, and verifies
all discovered destinations against that origin or explicit allowed internal
API origins. Secrets must not follow cross-origin redirects. Internal hosting
is supported; no official domain is required. Public origins use HTTPS,
without credentials, paths, queries or fragments; callbacks are exact approved
HTTPS URLs under the Agent origin. URL parsing/origin equality must be checked
in addition to schema shape checks.

The gateway does not fetch an administrator-supplied Agent URL. Pairing proves
possession of the approved code and new credential, not ownership of a DNS
domain. Agent installation setup requires its own bootstrap credential/local
CLI; it is not a second end-user identity system.

## Pairing and credential lifecycle

1. A gateway administrator approves Agent origin, exact callback and scopes.
   A high-entropy code is valid for at most 600 seconds; only its hash is stored.
2. Before redeem, Agent generates at least 32 random bytes and encodes them as
   an unpadded base64url Bearer token string. It encrypts and persists that
   string, credential ID, request ID and the pending operation locally.
3. `credential_hash` is lowercase hex SHA-256 of the UTF-8 bytes of the exact
   Bearer token string. Redeem sends this hash, not the reusable secret.
4. The gateway transaction consumes the code, creates a pending installation
   and credential, and stores the immutable normalized request and nonsecret
   result. JSON key order must not change replay identity. Same code/request
   ID/normalized fields returns the same pending result; altered fields give
   a conflict. Replays cannot extend expiration or resurrect revoked records.
5. Activate authenticates the actual Bearer token. A pending credential can
   only activate or inspect its own installation; it cannot use subject APIs.
6. An ambiguous response is recovered with the persisted request ID or
   authenticated self/status. Agent cannot invent another pending secret on
   retry. Lost local secret requires explicit abandonment and a new pairing.

Pending installations expire after at most 600 seconds. Activation uses a
transaction and uniqueness constraints to allow at most one ACTIVE installation
per Agent instance. A new pairing conflicts with an existing active one unless
a gateway administrator explicitly identifies the installation being replaced.
Replacement requires Agent to finish/drain or fence existing attempts before
activation revokes the old installation and its grants. Active attempts do not
keep two installations indefinitely.

Rotation registers a candidate hash under an active installation, then proves
possession with the candidate token. At most two usable credential generations
exist, with an overlap of at most 300 seconds. Concurrent requests require CAS
and idempotent request IDs; retry does not create another generation or restart
the overlap clock. Rotation changes client configuration generation, not the
installation ID, subject IDs, namespace or session epoch. Revoked credentials
never become valid again through rollback.

Codes, Bearer tokens, PKCE verifiers and provider credentials must not enter
logs, URLs, process arguments or fixtures containing real secrets. Gateway
stores only hashes of Agent service tokens; Agent encrypts its own tokens with
an Agent-owned deployment key.

## Identity, sessions and access

The BFF retains authorization code + PKCE S256 and validates state, signature,
issuer, client/audience, namespace and expiration before creating its session.
Redirect URIs use exact registration matching. The existing protocol is based
on [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636.html) and the redirect/PKCE
requirements in [RFC 9700](https://www.rfc-editor.org/rfc/rfc9700.html); pairing is
an application-specific installation protocol, not a claim of full OIDC support.

Identity v2, Agent session and access schemas allow only the fields named in
OpenAPI. They exclude email, names, global role, plan, balance and login secrets.
Session lifetime is at most 28,800 seconds. Cookies remain HttpOnly, Secure,
SameSite=Lax and origin-scoped, without a shared parent-domain cookie. A session
is an identity reference, not an eight-hour permission cache.

For each `(installation, subject)`, the gateway persists a monotonically
increasing `authorization_version`, including across revoke/re-consent. Codes,
identity claims, Agent sessions and issued grants bind to their authorization
version. A current allow response cannot upgrade an old session's version.
Version mismatch requires a new login. Ordinary credential rotation leaves
these subject versions unchanged. Downgraded policy is checked through current
access decisions even while a session is otherwise valid.

Access responses may be cached in memory for at most 30 seconds. Cache keys
include namespace, installation, subject and authorization version; config or
session-epoch invalidation discards affected entries. Measure a positive result's
deadline conservatively from the local monotonic request-start time, so network
delay does not extend the grant of access. No persistent stale-allow cache.
Writes, publishing, connections, exports and admin actions require a fresh
decision. Protected SSE/terminal/tool interactions revalidate within the same
deadline; polling interval plus timeout cannot extend it by another 30 seconds.

Gateway model requests independently check account status, installation state,
subject consent/version, grant state, allowed models and quota. Agent access
caches never replace this check. A failed lookup denies new protected work;
timeouts do not imply revocation or erase the saved binding.

Mint and renewal require current subject authorization. Cleanup may revoke an
owned grant after user consent is revoked. Usage returns only this installation's
own grant-attributed usage, including owned historical grants; it never exposes
the platform's global account ledger. The installation credential itself must
still be valid. Foreign grants return a stable unavailable response.
Grants narrow allowed models and lifetime; they introduce no per-task dollar
budget. Spend remains governed by the gateway's existing per-user quota.

## Agent binding state and process consistency

| Local state | Meaning |
|---|---|
| UNBOUND | Own services ready; setup protected; login/task APIs return `gateway_unbound` |
| PAIRING | Pending local operation is durable and recoverable; no user work |
| ACTIVE | Binding and authorization freshness allow work |
| DEGRADED | Retain binding; diagnose network/config errors; deny work without fresh authorization |
| DISCONNECTING | New work stopped; remote revocation not yet confirmed; retry durably |
| REVOKED | Explicit installation revocation observed; invalidate sessions and retain history/namespace |

`gateway_unbound` is an Agent-side error, not a newly promised Gateway endpoint.
Agent health/readiness checks its own process, DB, config and migrations;
integration status is separate. Gateway core readiness never probes Agent.

Agent owns `config_generation` and `binding_epoch`; they are not interchangeable
with a gateway installation version. Update the singleton binding with a
database CAS. Each API worker, scheduler and relay must observe the durable
generation at request/claim boundaries or through a bounded refresh policy that
also recovers from lost notifications. Each request uses one immutable snapshot.
JWKS caches are partitioned by issuer and generation; old clients close after
their in-flight users finish. An unbounded startup singleton is insufficient.

Installation polling and access freshness are bounded by 30 seconds. Explicit
installation revocation advances the local session epoch and stops work.
`credential_invalid` is a diagnosable credential failure, not proof that the
installation was revoked. The existing attempt lease/fencing protocol still
controls runner writes; reconnect must not reset fences or duplicate publishing.

For local disconnect, stop new work, claims and renewals first. Remain
DISCONNECTING until remote revocation is confirmed. Already-issued grants have
at most their remaining 900-second TTL, and cannot renew while disconnected.
In-flight model calls or already-running tool processes are not promised
instant cancellation; future work and interaction deadlines are enforced.

## Subject deletion and reconnect

An installation with the lifecycle scope may consume only its own operations.
Pull/ack must work even after the subject's account or consent is revoked; these
operations do not grant that subject permission to run tasks. A revoked
installation must use local administration or explicit gateway-admin transfer
of pending operations to a replacement installation of the same Agent instance
and namespace. A new credential alone cannot silently take over another queue.

Before purging, persist an operation/tombstone, block new subject work and
invalidate or fence existing writes, uploads, tool callbacks and publication
retries. Quiesce the subject's attempts using Agent's lease protocol. Clean
owned DB rows, sessions, encrypted Git/MCP credentials, object storage, workspaces,
sandboxes and indexes; repeated execution resumes safely. Only then acknowledge
completion. Record backup retention and restore-time deletion handling separately;
an online purge is not evidence that unexpired backups were physically erased.

Late runner callbacks must not recreate data after completion. Tombstones and
minimal operation audit survive cleanup as needed to reject those writes and
avoid repeating external effects. Disconnect and account suspension do not
themselves delete tasks. Gateway must report an offline/unconfirmed deletion as
pending, never as successful remote cleanup.

## Checking the draft

Use the existing Cloud Agent development validation tooling (`PyYAML`,
`openapi-spec-validator`, `jsonschema`); no Gateway runtime dependency is added.
Run from this repository root in an environment containing those tools:

```bash
python -m openapi_spec_validator contracts/cloud-agent-integration/v1/openapi.yaml
python - <<'PY'
import json
from pathlib import Path
import yaml
from jsonschema import Draft202012Validator, FormatChecker

root = Path("contracts/cloud-agent-integration/v1")
document = yaml.safe_load((root / "openapi.yaml").read_text())
fixtures = json.loads((root / "fixtures.json").read_text())
for group, expect_valid in (("valid", True), ("invalid", False)):
    assert fixtures[group], f"No {group} examples"
    for case in fixtures[group]:
        schema = {"$ref": f"#/components/schemas/{case['schema']}",
                  "components": document["components"]}
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        assert validator.is_valid(case["value"]) == expect_valid, case["name"]
print("OpenAPI fixtures accepted/rejected as declared")
PY
```

Schema checks cover shape, allowlists and scalar bounds. They do not prove JWT
signatures, equality with registered namespace/callback/client, `exp - iat`
limits, scope authorization, revocation, transaction uniqueness, idempotency,
multi-process refresh, purge fencing or a running deployment. Those require
provider/consumer and actual two-service tests in the implementation batches.

Provider storage work must cover the Gateway's promised PostgreSQL/D1 paths, or
explicitly gate an unsupported optional integration without breaking core
startup. Agent migrations stay in the Agent repository. The deployed compatibility
matrix and minimum safe rollback versions are populated only after real tests;
there is no claim that matching the proposed OpenAPI makes an old image compatible.
