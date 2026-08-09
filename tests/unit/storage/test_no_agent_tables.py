"""The gateway creates no cloud-agent tables (task H4 acceptance).

Eleven tables belonged to the agent job store, which left with the cloud agent.
Two stay, and only two: ``agent_grants``, because this gateway mints and
verifies the capability a sandbox calls models with, and ``identity_auth_codes``,
because it issues the codes a detached service exchanges for an identity.

Checked by reading the DDL rather than by booting against a database, so it
runs in the default tier. The `dbtest` tier does not run in CI — a fact this
migration learned the hard way, when a lifecycle test turned out to have been
broken for six days without anything noticing — so an acceptance criterion
parked there is an acceptance criterion nobody checks.

**A returning table is the symptom this exists for.** Nothing else would
notice: a `CREATE TABLE IF NOT EXISTS` pasted back in from an old branch
succeeds silently, and the gateway would quietly own agent state again, one
table at a time, with the cloud agent's own copy diverging beside it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "apps" / "backend"

#: The tables an agent-owning gateway had. Named individually rather than
#: matched by prefix: a prefix rule would also forbid the two below, and
#: loosening it to make room for them is how the rest come back.
DEPARTED_TABLES = (
    "agent_jobs",
    "agent_job_events",
    "agent_attempts",
    "agent_control_events",
    "agent_repo_grants",
    "agent_gitlab_connections",
    "agent_runner_hosts",
    "agent_threads",
    "agent_thread_messages",
    "agent_runner_policy",
    "agent_enroll_tokens",
)

#: What this gateway still owns, and why. Both are contract, not execution.
RETAINED_TABLES = {
    "agent_grants": "this gateway mints and verifies inference grants (C5)",
    "identity_auth_codes": "this gateway issues identity authorization codes (C2)",
}

_CREATE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.I)


def _created_tables() -> set[str]:
    """Every table name this backend's source asks a database to create."""
    names: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        names.update(match.lower() for match in _CREATE.findall(path.read_text(encoding="utf-8")))
    return names


@pytest.mark.parametrize("table", DEPARTED_TABLES)
def test_a_departed_agent_table_is_not_created_here(table: str) -> None:
    """It belongs to the cloud agent's database now, and to one schema owner."""
    assert table not in _created_tables(), (
        f"{table} is created by this gateway again. It moved to the cloud agent "
        "at H4; two owners of one table is two schemas that drift."
    )


@pytest.mark.parametrize(("table", "why"), sorted(RETAINED_TABLES.items()))
def test_a_retained_table_is_still_created_here(table: str, why: str) -> None:
    """The other direction, and the more useful one.

    A sweep that removed agent tables enthusiastically would take these too,
    and the symptom is not a missing table — it is sign-in failing, or every
    grant mint answering 500, some deploys later.
    """
    assert table in _created_tables(), f"{table} must still be created here: {why}"


def test_no_agent_table_is_created_that_this_file_does_not_name() -> None:
    """Catches a *new* agent table, which neither list above would.

    The failure mode this migration exists to prevent is the gateway growing
    agent state back. A name nobody chose to allow should fail here and be an
    explicit decision, not an omission.
    """
    unexpected = sorted(
        name
        for name in _created_tables()
        if name.startswith("agent_") and name not in RETAINED_TABLES
    )
    assert not unexpected, (
        f"this gateway creates agent tables that H4 did not sanction: {unexpected}. "
        "Agent state lives in the cloud agent's database."
    )
