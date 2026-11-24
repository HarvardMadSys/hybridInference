"""Bootstrap starts the managed routers last, and stops for one that cannot serve safely.

``_start_managed_routers`` is the loop ``initialize()`` runs once everything else
is up. A greedy router whose engine queues cannot be registered must stop the
gateway: it would otherwise raise the same error on its first request, and its
deadlines mean nothing without the queue.
"""

from __future__ import annotations

import pytest

from serving.adapters.upstream_limiter import EngineQueueConflict, EngineQueueUnavailable
from serving.servers.bootstrap import _start_managed_routers


class _Router:
    """A managed router whose start can be made to fail."""

    def __init__(self, fail_with: BaseException | None = None) -> None:
        self.fail_with = fail_with
        self.started = False

    async def start(self) -> bool:
        if self.fail_with is not None:
            raise self.fail_with
        self.started = True
        return True

    async def stop(self) -> bool:
        return False


class _Registry:
    def runtime_override_for_router(self, router: object) -> None:
        return None


@pytest.mark.parametrize(
    "error",
    [
        EngineQueueConflict("engine localhost:18003 given two limits"),
        EngineQueueUnavailable("several worker processes"),
    ],
)
async def test_a_router_that_cannot_register_its_engine_queues_stops_startup(error):
    first, failing, later = _Router(), _Router(fail_with=error), _Router()

    with pytest.raises(type(error)):
        await _start_managed_routers([first, failing, later], _Registry())  # type: ignore[list-item]

    assert (first.started, later.started) == (True, False)


async def test_any_other_start_failure_is_logged_and_startup_goes_on():
    failing, later = _Router(fail_with=RuntimeError("probe down")), _Router()

    await _start_managed_routers([failing, later], _Registry())  # type: ignore[list-item]

    assert later.started
