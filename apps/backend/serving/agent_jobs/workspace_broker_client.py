"""Gateway-side client for the private agent workspace broker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class WorkspaceBrokerError(RuntimeError):
    """A bounded, owner-safe failure returned by the internal broker."""

    status_code: int
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass
class WorkspaceBrokerStream:
    """An already-authorized broker response that the gateway must close."""

    _client: httpx.AsyncClient
    _response: httpx.Response

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._response.aiter_raw()

    async def aclose(self) -> None:
        """Release both the streaming response and its dedicated client."""
        try:
            await self._response.aclose()
        finally:
            await self._client.aclose()


class WorkspaceBrokerClient:
    """Talk to the broker without exposing its credential to the browser."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Agent-Workspace-Token": token}

    @staticmethod
    def _response_error(response: httpx.Response) -> WorkspaceBrokerError:
        """Read a bounded, owner-safe error from a completed broker response."""
        message = "The live workspace request failed."
        try:
            body = response.json()
            detail = body.get("detail") if isinstance(body, dict) else None
            if isinstance(detail, str) and detail:
                message = detail
        except ValueError:
            pass
        return WorkspaceBrokerError(response.status_code, message)

    async def _request(
        self,
        method: str,
        workspace_id: str,
        suffix: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}/workspaces/{workspace_id}/{suffix}",
                    headers=self._headers,
                    params=params,
                    json=json,
                )
        except httpx.HTTPError as exc:
            raise WorkspaceBrokerError(
                503, "The live workspace is temporarily unavailable."
            ) from exc
        if response.is_error:
            raise self._response_error(response)
        body = response.json()
        if not isinstance(body, dict):
            raise WorkspaceBrokerError(502, "The live workspace returned an invalid response.")
        return body

    async def files(self, workspace_id: str, path: str) -> dict[str, Any]:
        """Read one file or directory from a live workspace."""
        return await self._request("GET", workspace_id, "files", params={"path": path})

    async def write_file(self, workspace_id: str, path: str, content: str) -> dict[str, Any]:
        """Replace one UTF-8 file in a live workspace."""
        return await self._request(
            "PUT", workspace_id, "files", params={"path": path}, json={"content": content}
        )

    async def terminal(
        self, workspace_id: str, *, command: str, cwd: str, timeout_seconds: float
    ) -> dict[str, Any]:
        """Execute one bounded command in an isolated workspace sandbox."""
        return await self._request(
            "POST",
            workspace_id,
            "terminal",
            json={"command": command, "cwd": cwd, "timeout_seconds": timeout_seconds},
            timeout=timeout_seconds + 10,
        )

    async def create_terminal(self, workspace_id: str, *, rows: int, cols: int) -> dict[str, Any]:
        """Open one interactive terminal in a durable workspace."""
        return await self._request(
            "POST", workspace_id, "terminals", json={"rows": rows, "cols": cols}
        )

    async def list_terminals(self, workspace_id: str) -> dict[str, Any]:
        """List the interactive terminals retained by a workspace."""
        return await self._request("GET", workspace_id, "terminals")

    async def suspend_terminals(
        self, workspace_id: str, *, lease_generation: int
    ) -> dict[str, Any]:
        """Freeze every interactive terminal process tree in a workspace."""
        return await self._request(
            "POST",
            workspace_id,
            "terminals/suspend",
            json={"lease_generation": lease_generation},
            timeout=60,
        )

    async def resume_terminals(self, workspace_id: str, *, lease_generation: int) -> dict[str, Any]:
        """Resume retained interactive terminals after a protected phase."""
        return await self._request(
            "POST",
            workspace_id,
            "terminals/resume",
            json={"lease_generation": lease_generation},
            timeout=60,
        )

    async def resume_settled_terminals(self, workspace_id: str) -> dict[str, Any]:
        """Authoritatively resume terminals after the job can no longer retry."""
        return await self._request(
            "POST",
            workspace_id,
            "terminals/resume-settled",
            timeout=60,
        )

    async def stream_terminal(
        self, workspace_id: str, terminal_id: str, *, after: int
    ) -> WorkspaceBrokerStream:
        """Open and validate a terminal SSE stream before public headers are sent."""
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        )
        suffix = f"terminals/{quote(terminal_id, safe='')}/stream"
        response: httpx.Response | None = None
        ownership_transferred = False
        try:
            request = client.build_request(
                "GET",
                f"{self._base_url}/workspaces/{workspace_id}/{suffix}",
                headers=self._headers,
                params={"after": str(after)},
            )
            response = await client.send(request, stream=True)
            if response.is_error:
                await response.aread()
                raise self._response_error(response)
            ownership_transferred = True
            return WorkspaceBrokerStream(client, response)
        except WorkspaceBrokerError:
            raise
        except httpx.HTTPError as exc:
            raise WorkspaceBrokerError(
                503, "The live workspace is temporarily unavailable."
            ) from exc
        finally:
            # ``asyncio.CancelledError`` is a BaseException, so exception-only
            # cleanup leaks this dedicated client while a cancelled public SSE
            # request is still connecting. Ownership moves to the returned
            # stream only after a successful, validated response.
            if not ownership_transferred:
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    await client.aclose()

    async def terminal_input(
        self, workspace_id: str, terminal_id: str, *, data: str
    ) -> dict[str, Any]:
        """Write base64-encoded bytes to an interactive terminal."""
        suffix = f"terminals/{quote(terminal_id, safe='')}/input"
        return await self._request("POST", workspace_id, suffix, json={"data": data})

    async def resize_terminal(
        self, workspace_id: str, terminal_id: str, *, rows: int, cols: int
    ) -> dict[str, Any]:
        """Resize an interactive terminal."""
        suffix = f"terminals/{quote(terminal_id, safe='')}/resize"
        return await self._request("POST", workspace_id, suffix, json={"rows": rows, "cols": cols})

    async def delete_terminal(self, workspace_id: str, terminal_id: str) -> dict[str, Any]:
        """Kill an interactive terminal; the private operation is idempotent."""
        suffix = f"terminals/{quote(terminal_id, safe='')}"
        return await self._request("DELETE", workspace_id, suffix)

    async def git(self, workspace_id: str, base_sha: str | None = None) -> dict[str, Any]:
        """Read live status, base-relative diff, and recent commits."""
        params = {"base_sha": base_sha} if base_sha else None
        return await self._request("GET", workspace_id, "git", params=params, timeout=70)


def workspace_broker_from_env() -> WorkspaceBrokerClient | None:
    """Return the configured broker, or ``None`` for legacy deployments."""
    url = (os.environ.get("AGENT_WORKSPACE_BROKER_URL") or "").strip()
    token = (os.environ.get("AGENT_WORKSPACE_BROKER_TOKEN") or "").strip()
    if not url or not token:
        return None
    return WorkspaceBrokerClient(url, token)


__all__ = [
    "WorkspaceBrokerClient",
    "WorkspaceBrokerError",
    "WorkspaceBrokerStream",
    "workspace_broker_from_env",
]
