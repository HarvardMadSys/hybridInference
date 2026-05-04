"""Pinned Alembic head revision for the runtime boot guard.

Bumped per schema-change PR — when a developer adds a new migration file,
they update ``EXPECTED_ALEMBIC_VERSION`` here to the new revision id in
the same PR. ``serving.servers.bootstrap._verify_schema_version`` reads
this value at startup and refuses to boot if the live DB's
``alembic_version`` does not match.
"""

EXPECTED_ALEMBIC_VERSION = "0001_baseline"
