"""Gateway-side client for the private agent workspace broker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class WorkspaceBrokerError(RuntimeError):
    """A bounded, owner-safe failure returned by the internal broker."""

    status_code: int
    message: str

    def __str__(self) -> str:
        return self.message


class WorkspaceBrokerClient:
    """Talk to the broker without exposing its credential to the browser."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Agent-Workspace-Token": token}

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
            message = "The live workspace request failed."
            try:
                body = response.json()
                detail = body.get("detail") if isinstance(body, dict) else None
                if isinstance(detail, str) and detail:
                    message = detail
            except ValueError:
                pass
            raise WorkspaceBrokerError(response.status_code, message)
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


__all__ = ["WorkspaceBrokerClient", "WorkspaceBrokerError", "workspace_broker_from_env"]
