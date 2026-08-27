# Backend-coupled database analysis tools

The operational database toolkit that used to live here — backup, restore,
archive and export scripts plus their cron files, the trace viewer, and the
per-deployment analysis one-offs — is owned and run by the consuming
distribution repository since the W5d closeout; production cron already
executes that copy. This directory keeps only the analysis tools that import
the gateway backend (`serving.*`) at the top level and therefore only work
next to this repository's `apps/backend`:

- `analysis/user_automation_score.py` — scores how automated a user's traffic
  looks (documented in `docs/developer/automation-score.md`)
- `analysis/user_prompt_sample.py` — samples prompts via
  `serving.utils.prompt_sampling`
- `analysis/geo_hourly_export.py` + `analysis/geo_globe.html` — hourly
  geo-demand export and its standalone globe viewer
- `backfill_num_user_turns.py` — backfills `api_logs.num_user_turns` using
  `serving.storage.utils.conversation_shape`

All of them read a Postgres DSN from the environment (`DB_*` / `DATABASE_URL`)
and are safe to run read-only against a replica; the backfill is the one
writer.
