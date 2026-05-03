Prometheus Stack (Removed)
==========================

> **Status: removed.** Prometheus is no longer part of the active deployment
> stack and the team is moving to Slack-based alerting. The configuration
> files in this directory are retained for git history only — they are not
> wired into any running service. Do not treat the layout, quick-start, or
> remote-target guidance that previously appeared here as current operational
> guidance.

For the replacement alerting design and migration plan, see:

- `docs/superpowers/specs/2026-05-02-alert-management-cleanup-design.md`
- `docs/source/developer/deployment.md` (Monitoring / Alerting section)

If you need to revive a metrics pipeline, treat the YAML files here as a
starting reference and re-validate them against the current codebase before
deploying.
