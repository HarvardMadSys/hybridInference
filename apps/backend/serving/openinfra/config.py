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

Publishing is the distribution's ``features.openinfra`` (:func:`published`).
"""

from __future__ import annotations

from serving.config.app_config import config_value
from serving.config.distribution import DistributionConfigError, get_active_distribution_config

INGEST_TOKEN = "OPENINFRA_INGEST_TOKEN"
ENGINE_ORDER = "OPENINFRA_ENGINE_ORDER"
RETIRED_ENGINES = "OPENINFRA_RETIRED_ENGINES"


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
