# Served identity diagnostic entitlement decision

Issue #1609 requires served-identity response headers to remain concealed by
default and requires an authenticated caller to hold a server-authorized
diagnostic entitlement as well as request attribution on that call. The
existing internal/admin role and API-key authentication are not a specific
diagnostic entitlement, so neither is sufficient by itself.

The response-header policy now accepts an explicit `served_identity` entry in
the authenticated server-side `diagnostic_entitlements` context and requires
`X-FI-Served-Identity: include` on that request. Agent-grant contexts remain
ineligible. The client header cannot create the context entitlement. Trusted
synthetic probes continue through their existing independent authorization
path.

**Remaining blocker:** the repository has no approved key/account entitlement
field or administrative grant path for `served_identity`. Until that security
policy and grant mechanism are approved and implemented, normal authenticated
callers receive no served-identity headers, even when they send the opt-in.
Do not infer this entitlement from an API key, role, or client-controlled
metadata. This safe foundation intentionally leaves production opt-in
unavailable pending that decision.
