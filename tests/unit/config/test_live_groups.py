"""A live group's keys reach their readers only through the group.

``app_config_registry.MANAGED_GROUPS`` hands keys to a live configuration group
only once every reader on the execution path takes the group's published view.
A reader that reads one of those ``Settings`` fields, or the raw value through
``config_value``, would see a value the group has not published (or no longer
publishes) and break the group's one-view-per-use guarantee.
"""

from __future__ import annotations

import re
from pathlib import Path

from serving.config import app_config_registry as registry
from serving.config.app_config_registry import ALERTS_RUNTIME_GROUP, MANAGED_GROUPS, static_entry

BACKEND = Path(__file__).resolve().parents[3] / "apps" / "backend" / "serving"

#: Where a group's fields may be named: the settings model that validates them
#: and the group's own component.
OWNERS = {
    ALERTS_RUNTIME_GROUP: {
        BACKEND / "config" / "settings.py",
        BACKEND / "observability" / "alert_runtime.py",
    },
}


def _sources() -> list[Path]:
    return [path for path in BACKEND.rglob("*.py") if "__pycache__" not in path.parts]


def test_every_owned_key_is_a_restart_required_setting() -> None:
    # Restart-required is how an owned key behaves until its group is active.
    for contract in MANAGED_GROUPS.values():
        for key in contract.keys:
            entry = static_entry(key)
            assert entry is not None and entry.field is not None, key
            assert entry.restart_required, key
            assert registry.managed_group(key) == contract.name


def test_no_reader_outside_the_group_reads_an_owned_field() -> None:
    offenders = []
    for name, contract in MANAGED_GROUPS.items():
        fields = [static_entry(key).field for key in contract.keys]
        attribute = re.compile(
            r"settings\b[^\n]*?\.({})\b".format("|".join(map(re.escape, fields)))
        )
        by_name = re.compile(
            r"getattr\([^)]*[\"']({})[\"']".format("|".join(map(re.escape, fields)))
        )
        raw = re.compile(
            r"\bconfig_value\(\s*[\"']({})[\"']".format("|".join(map(re.escape, contract.keys)))
        )
        for path in _sources():
            if path in OWNERS[name]:
                continue
            text = path.read_text()
            for pattern in (attribute, by_name, raw):
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(f"{path.relative_to(BACKEND)}:{line}: {match.group(0)}")
    assert offenders == []
