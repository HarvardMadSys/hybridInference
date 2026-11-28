# Backend-coupled database analysis tools

The operational database toolkit (backup, restore, archive and export scripts
and their schedules, the trace viewer, per-deployment one-off analyses) lives in
the repository of the deployment that runs it. This directory keeps only the
tools that import the gateway backend (`serving.*`) at the top level, so they
work only next to this repository's `apps/backend`:

| Tool | Purpose |
|---|---|
| `analysis/user_automation_score.py` | Scores how automated a user's traffic looks; see `docs/developer/automation-score.md` |
| `analysis/user_prompt_sample.py` | Samples prompts through `serving.utils.prompt_sampling` |
| `analysis/geo_hourly_export.py`, `analysis/geo_globe.html` | Hourly geographic-demand export and its standalone globe viewer |
| `backfill_num_user_turns.py` | Backfills `api_logs.num_user_turns` with `serving.storage.utils.conversation_shape` |

All of them build a Postgres DSN from the `DB_*` environment variables. Only the
backfill writes; the others are safe to run read-only against a replica.
