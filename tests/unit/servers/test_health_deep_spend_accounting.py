"""``/health/deep`` reports the spend accounting mode and, to an administrator, its numbers.

In the shared mode an operator needs to see, without a database session, how
long admissions take, whether any were refused for the database's sake, what
this process holds and for how long, and whether its settlements are being
written. They name no account, but they are operating figures: only an
administrator is shown them, and they never change ``status``.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi import Response
from fastapi.encoders import jsonable_encoder

from serving.servers.routers.health import deep_health
from serving.shared_spend import ledger as shared_ledger, mode as spend_mode
from serving.shared_spend.ledger import SharedSpendLedger
from serving.spend_ledger import SpendCheck
from tests.fixtures.shared_spend import FakeSharedBackend


class _StubRouter:
    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        return {}


async def _deep(**caller: Any) -> dict[str, Any]:
    return await deep_health(
        Response(),
        router_exec=_StubRouter(),
        services=SimpleNamespace(routing_manager=None),
        op_store=None,
        log_store=None,
        **caller,
    )


async def test_the_per_process_mode_is_named_and_has_nothing_more_to_report():
    assert (await _deep(admin=True))["spend_accounting"] == {"mode": "local"}
    assert (await _deep())["spend_accounting"] == {"mode": "local"}


async def test_an_administrator_sees_what_this_processs_shared_ledger_observed(monkeypatch):
    monkeypatch.setattr(spend_mode, "_mode", "shared")
    backend = FakeSharedBackend()
    ledger = SharedSpendLedger(backend, instance_id="inst-health")
    monkeypatch.setattr(shared_ledger, "_LEDGERS", {"the store": ledger})
    check = SpendCheck(
        ledger=ledger,
        user_id="u-health",
        key_id=None,
        read_at=time.monotonic(),
        account_quota_usd=1.0,
        account_spent_usd=0.0,
    )
    reservation, _ = await ledger.admit(check, 0.30)
    # What its hourly survey found in the database, every process's included.
    backend.open_rows = [
        {
            "instance_id": "inst-dead",
            "state": "active",
            "n": 2,
            "amount_usd": Decimal("0.60"),
            "oldest_age_seconds": 90.0,
            "owner": "stale",
        }
    ]
    await ledger._keep_up()

    body = await _deep(admin=True)
    (stats,) = body["spend_accounting"]["ledgers"]
    assert body["spend_accounting"]["mode"] == "shared"
    assert stats["instance_id"] == "inst-health"
    assert stats["admissions"] == {"admitted": 1}
    assert stats["admission_latency"]["count"] == 1
    assert stats["held"]["active"]["reservations"] == 1
    assert stats["settlement"]["failing_for_seconds"] is None
    assert stats["database"]["stranded"]["reservations"] == 2
    assert "u-health" not in json.dumps(jsonable_encoder(body))  # serializable, and names nobody
    assert body["status"] == "healthy"  # what a ledger holds is no reason to fail a probe

    # Anyone else is told the mode, and no more.
    assert (await _deep())["spend_accounting"] == {"mode": "shared"}
    assert (await _deep(admin=False))["spend_accounting"] == {"mode": "shared"}
    reservation.release()
    assert await ledger.flush(5)
