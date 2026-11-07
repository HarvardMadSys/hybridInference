"""Alert history: a best-effort, PostgreSQL-only record of what the alerting did.

Every backend alert path (rule breaches, recoveries, suppression decisions,
delivery attempts and their results, admin test sends) is turned into
allowlisted, redacted history records at the point it happens, offered to a
bounded in-memory queue without waiting, and written to PostgreSQL by one
background writer per process. Other sources (the status monitor) submit their
own records through ``POST /internal/alert-history/events``.

Only a committed PostgreSQL row is saved history. A record in the queue may be
lost to a crash, a full buffer or a shutdown that runs out of time, so every
read reports ``unknown_gaps_possible``. See ``docs/developer/alert-history.md``.

Nothing here may change what the alerting does: every hook swallows its own
failures, and the notification path never waits on the database.
"""
