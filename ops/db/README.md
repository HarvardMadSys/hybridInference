# Backend-coupled database analysis tools

The operational database toolkit — backup, restore, archive and export scripts
with their schedules, the trace viewer, and per-deployment analysis one-offs —
lives in the repository of the deployment that runs it. This directory keeps
only the analysis tools that import the gateway backend (`serving.*`) at the
top level and therefore only work next to this repository's `apps/backend`:

- `analysis/user_automation_score.py` — scores how automated a user's traffic
  looks (documented in `docs/developer/automation-score.md`)
- `analysis/user_prompt_sample.py` — samples prompts via
  `serving.utils.prompt_sampling`
- `analysis/geo_hourly_export.py` + `analysis/geo_globe.html` — hourly
  geo-demand export and its standalone globe viewer
- `backfill_num_user_turns.py` — backfills `api_logs.num_user_turns` using
  `serving.storage.utils.conversation_shape`

All of them build a Postgres DSN from the `DB_*` environment variables
and are safe to run read-only against a replica; the backfill is the one
writer.
