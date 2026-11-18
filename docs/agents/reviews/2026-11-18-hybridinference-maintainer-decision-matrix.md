# Maintainer decision matrix — PRs #1782, #1781, #1786

These are product and release-policy choices for maintainers. No choice is made by this qualification report. The PRs remain unmerged; CI still awaits authorization.

| PR / question | Available choices | Consequence to confirm |
|---|---|---|
| #1782 — replacement of #1733 | Accept #1782 as the replacement, or retain #1733 as the canonical change and request a different integration plan. | Acceptance makes #1782 the maintained claim-lease implementation. Preserve #1733's review history and close it only after replacement acceptance. Rejection leaves competing implementations unresolved. |
| #1782 — migration ownership and sequence | Accept #1782 owning `0016_hard_delete_claim_lease` and `0022_hard_delete_merge`, followed by #1781's `0023_alert_preload_indexes`; or specify an alternate owner/sequence before merge. | The current sequence yields one Alembic head and avoids merging #1781 before its parent mergepoint. An alternative must preserve a valid graph and upgrade path. |
| #1781 — startup history restoration | Accept bounded best-effort restoration, or require restoration to succeed before startup proceeds. | Best-effort startup keeps the service available but cannot guarantee recovery delivery for history that could not be restored. A startup requirement strengthens recovery expectations but can make unavailable history a startup blocker. |
| #1781 — record-only alert when history persistence fails | Fail open and send the alert to the notification channel, or fail closed and suppress notification until history succeeds. | Fail open preserves alert visibility but may produce a notification without a durable history record and can complicate deduplication. Fail closed preserves the record/notification coupling but risks losing a live alert during storage failure. |
| #1786 — API keys eligible for `served_identity` | Keep explicit admin-set `api_keys.metadata.diagnostic_entitlements` plus the request header as the entitlement, or restrict eligible keys to a named class (for example, staff/admin/service diagnostics keys). | Broader explicit entitlement is easier to grant but increases the set of callers who can receive serving identity metadata. A restricted class narrows disclosure and requires a clear key lifecycle/administration rule. |
| #1786 — privileged role required | Require the authenticated principal to hold a privileged role in addition to the key entitlement, or rely on the explicit key entitlement alone. | Role gating narrows access and couples disclosure to role state; entitlement-only access is simpler but makes key metadata the controlling authorization boundary. |
| #1786 — `X-Probe` endpoint identifiers | Permit dedicated endpoint identifiers on the probe response, or suppress those identifiers on `X-Probe` while retaining them on the approved inference response. | Disclosure helps diagnose which dedicated endpoint served a probe but exposes endpoint identity to probe callers. Suppression reduces diagnostic detail and endpoint disclosure. |
| All three — exact-head CI | Authorize the protected workflows, or hold authorization pending further review. | Authorization creates actual workflow jobs whose required results can be evaluated. Until jobs execute, `action_required` is not a pass. |
| All three — formal review | Approve after review, or request changes with specific findings. | Current review events are comments, not approvals; merge requirements remain unsatisfied until the repository's formal review rules are met. |

## Current technical evidence relevant to the decisions

- The candidate and clean `dev` broad database suites both have failures; no full-suite green result is claimed.
- The index-only visibility assertion reproduced on clean `dev` under matched parallel load. Its underlying visibility-map state remains a baseline test/environment issue, not a proven PR regression.
- The shared-spend admission timeout remains unresolved. Matched targeted traces passed on both trees, which is not proof that the original broad-run occurrence was harmless.
- No policy decision should be inferred from CI or local test results.

## Required migration and merge sequence

Merge #1782 before #1781 if maintainers accept the current ownership. #1786 is independent. Do not close superseded PRs #1733, #1725, or #1609 until their replacements are accepted and their review history is preserved.
