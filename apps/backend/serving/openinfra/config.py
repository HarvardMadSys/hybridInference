"""OpenInfra's settings, read per request so an Admin change applies within seconds.

- ``OPENINFRA_INGEST_TOKEN``: the bearer token reporters present. Empty turns
  ingestion off: ``POST /internal/openinfra/ingest`` answers 404.
- ``OPENINFRA_ENGINE_ORDER``: engine names, comma-separated, in the order the
  page shows their tabs. Engines not listed follow, alphabetically.
- ``OPENINFRA_RETIRED_ENGINES``: engine ids kept out of the live views, the
  served totals, the history and exports. What they reported stays stored, so
  removing an id brings it back. Retire an id explicitly rather than inferring
  that one engine replaced another: one endpoint may serve several models, and
  the same localhost endpoint repeats across hosts.
- ``OPENINFRA_RETENTION_DAYS``: how many days samples and request events stay
  in the database (7 by default; 0 keeps them all).
- ``OPENINFRA_ARCHIVE_DIR``: where each ended week is archived before it is
  deleted (:mod:`serving.openinfra.archive`). An environment setting, as a
  path on the gateway's own filesystem is: unset, old rows are deleted
  unarchived.

Publishing is the distribution's ``features.openinfra`` (:func:`published`).
"""

from __future__ import annotations

import os
from pathlib import Path

from serving.config.app_config import config_value
from serving.config.distribution import DistributionConfigError, get_active_distribution_config

INGEST_TOKEN = "OPENINFRA_INGEST_TOKEN"
ENGINE_ORDER = "OPENINFRA_ENGINE_ORDER"
RETIRED_ENGINES = "OPENINFRA_RETIRED_ENGINES"
RETENTION_DAYS = "OPENINFRA_RETENTION_DAYS"
ARCHIVE_DIR = "OPENINFRA_ARCHIVE_DIR"
#: The retention while the setting is unset or unreadable.
DEFAULT_RETENTION_DAYS = 7


def published() -> bool:
    """Whether the active distribution publishes OpenInfra (``features.openinfra: true``)."""
    try:
        distribution = get_active_distribution_config()
    except DistributionConfigError:
        return False
    return distribution is not None and distribution.features.openinfra is True


def ingest_token() -> str:
    """The configured ingest token, or ``""`` when ingestion is off."""
    return (config_value(INGEST_TOKEN) or "").strip()


def _names(key: str) -> tuple[str, ...]:
    raw = config_value(key) or ""
    return tuple(dict.fromkeys(name.strip() for name in raw.split(",") if name.strip()))


def engine_order() -> tuple[str, ...]:
    """Engine names in tab order, as configured; empty means alphabetical."""
    return _names(ENGINE_ORDER)


def retired_engines() -> tuple[str, ...]:
    """Engine ids left out of everything the routes serve."""
    return _names(RETIRED_ENGINES)


def retention_days() -> int:
    """Days samples and request events are kept; 0 keeps them all."""
    raw = (config_value(RETENTION_DAYS) or "").strip()
    try:
        days = int(raw) if raw else DEFAULT_RETENTION_DAYS
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return max(days, 0)


def archive_dir() -> Path | None:
    """The directory weeks are archived to, or ``None`` when there is none."""
    raw = os.environ.get(ARCHIVE_DIR, "").strip()
    return Path(raw) if raw else None
