"""No query compares a timestamptz column against a zone-less UTC truncation.

``date_trunc('day', NOW() AT TIME ZONE 'UTC')`` is a timestamp without a zone;
compared with ``api_logs.timestamp`` it is read in the session's time zone, so
"today" moved with the database's ``timezone`` setting (the daily quota read
0.0 or 11.0 for a true 1.0; tests/integration/storage/test_postgres_utc_windows.py).
Convert it back with ``AT TIME ZONE 'UTC'``, or format it with ``to_char`` for
a UTC day string. Likewise truncate a timestamptz column in UTC
(``date_trunc('hour', timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'``).
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[3] / "apps" / "backend" / "serving"

# Adjacent string literals ("...date_trunc('day', "\n  f"NOW() ...") read as one.
_JOIN = re.compile(r"\"\s*\n\s*f?\"")
_ZONELESS_BOUND = re.compile(
    r"(?<!to_char\()(?<!to_char\(\()date_trunc\('[a-z{}]+',\s*NOW\(\) AT TIME ZONE 'UTC'\)"
    r"(?!\s*AT TIME ZONE 'UTC')"
)
_SESSION_ZONE_TRUNC = re.compile(r"date_trunc\('[a-z]+',\s*(?:\w+\.)?timestamp\)")


def _problems(text: str) -> list[str]:
    joined = _JOIN.sub("", text)
    found = []
    for pattern in (_ZONELESS_BOUND, _SESSION_ZONE_TRUNC):
        found += [match.group(0) for match in pattern.finditer(joined)]
    return found


def test_utc_windows_carry_their_zone() -> None:
    problems = []
    for path in sorted(SOURCE.rglob("*.py")):
        for found in _problems(path.read_text(encoding="utf-8")):
            problems.append(f"{path.relative_to(SOURCE.parents[2])}: {found}")
    assert not problems, "\n  ".join(["Zone-dependent UTC window:", *problems])


def test_the_scan_tells_the_forms_apart() -> None:
    assert _problems("timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')")
    # Split across two adjacent literals, as long f-strings are.
    assert _problems("x >= date_trunc('day', \"\n    f\"NOW() AT TIME ZONE 'UTC')")
    assert _problems("WHERE date_trunc('hour', timestamp) = $1")
    assert _problems("date_trunc('hour', l.timestamp)")
    assert not _problems("(date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')")
    assert not _problems("to_char(date_trunc('month', NOW() AT TIME ZONE 'UTC'), 'YYYY-MM-DD')")
    assert not _problems("date_trunc('hour', timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'")
