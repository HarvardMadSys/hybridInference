"""Regression tests for the gateway's private workspace-broker client."""

from __future__ import annotations

import asyncio

import pytest

from serving.agent_jobs import workspace_broker_client
from serving.agent_jobs.workspace_broker_client import WorkspaceBrokerClient


@pytest.mark.asyncio
async def test_cancelled_terminal_connect_closes_its_dedicated_http_client(monkeypatch):
    """Cancelling during ``send`` must not leak the unowned streaming client."""

    class _BlockedClient:
        def __init__(self, *args, **kwargs) -> None:
            self.started = asyncio.Event()
            self.closed = False

        @staticmethod
        def build_request(*args, **kwargs):
            return object()

        async def send(self, request, *, stream: bool):
            self.started.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    created: list[_BlockedClient] = []

    def make_client(*args, **kwargs):
        client = _BlockedClient(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(workspace_broker_client.httpx, "AsyncClient", make_client)
    broker = WorkspaceBrokerClient("http://broker", "secret")
    task = asyncio.create_task(broker.stream_terminal("ajob_test", "term_test", after=0))
    await asyncio.sleep(0)
    await created[0].started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert created[0].closed is True
