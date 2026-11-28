# Database-only application configuration

Status: implemented. For the settings reference, see
[Configuration](../../developer/configuration.md).

## Decision

Application configuration lives in the database, not in files. The model
catalog, routing policy, alert rules, site identity and public branding are
JSON documents in the existing `app_config` store (`MODEL_CATALOG`,
`ROUTING_POLICY`, `ALERT_RULES`, `SITE_CONFIGURATION`, `SITE_BRANDING`). They
share the store's validation, conditional writes, audit, administrator
reauthentication, backup and restore, and status reporting. No application
file is a runtime fallback. Docker Compose and CI stay YAML.

A fresh database serves a neutral console, first-run setup and an "Ask an admin
to configure" state. It has no bundled provider and assumes no model.

## Migration from files

- An expand migration copies existing `route_catalog` documents into settings
  without overwriting administrator settings. Embedded credentials become
  separate secret settings that the documents reference.
- The old rows stay so the previous release keeps working, and later saves of
  model and routing documents are mirrored into them. The new gateway never
  reads them.
- Nothing imports external YAML overlays any more. `import-legacy`, which
  imported them once, was retired with the variables that named the files;
  the last release that read them was 20261107.1. A deployment upgrading from
  one stores its documents in Admin or with
  `python -m serving.config.manage document`. Normal startup never scans those
  files.

## Runtime rules

- Fixed and RouteWise execution, lifecycle, reservations and feedback ownership
  are unchanged. Routing synchronization uses scoped transitions of the
  existing router objects. Models it does not touch, and requests in flight,
  keep their original owner.
- Admin reports saved state and applied state separately. A database commit
  does not prove that every worker applied the change.
- Document validation rejects an invalid write before it is published. If a
  runtime refresh fails, the failure is reported and retried. A routing row
  that was corrupted by hand is not guaranteed to keep the previous effective
  route view. A prepare/publish rollback boundary for those rows is not built.

## Operations

Deployment uses pinned artifacts with migrations run before switchover, then
readiness and smoke checks. Database recovery is a separate, explicit drill.
