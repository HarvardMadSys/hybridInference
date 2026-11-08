# Database-only application configuration

Date: 2026-11-08. Status: accepted for implementation at the user's request.

This supersedes file resolution and boot-time YAML import in the distribution,
route-catalog and configuration designs. Docker Compose and CI remain YAML.

Application documents (model catalog, routing policy, alert rules, site identity
and public branding) are JSON values in the existing `app_config` store. They
share its validation, conditional writes, audit, administrator reauthentication,
backup/restore and status reporting. No application file is a runtime fallback.
A fresh database serves a neutral console, first-run setup and an explicit
“Ask an admin to configure” state, with no bundled provider or assumed model.

An expand migration copies existing route_catalog documents into settings,
without overwriting administrator settings. Embedded credentials become separate
secret settings referenced by the documents. Old rows stay for previous-release
compatibility and mirror later model/routing document saves; the new gateway
never reads them. An explicit offline import command carries existing external
YAML overlays into the database; normal startup never scans those files.

Preserve Fixed and RouteWise execution, lifecycle, reservations and feedback
ownership. Runtime routing synchronization must use scoped existing transitions
and leave unaffected models and requests in flight with their original owners.
Report saved and applied state separately; a database commit alone is not proof
that all workers applied a change. Application document validation rejects invalid
writes before publication. Runtime refresh failures are reported and retried;
manually corrupted routing rows are not guaranteed to retain the previous
effective route view. A prepare/publish rollback boundary for those rows remains
deferred.

Operations also gain diagnostics and generic smoke commands, durable Compose
options, and pinned-artifact deployment/rollback with migrations before switchover
and readiness/smoke acceptance. Database recovery is a separate explicit drill.

Verification: focused unit and API tests; migration and restore tests against
local PostgreSQL; frontend checks; format/lint/default tests; an isolated running
gateway demonstrating empty configuration, Admin configuration, inference,
backup/restore, and multi-process update behavior.
