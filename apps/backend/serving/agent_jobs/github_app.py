"""GitHub App credentials for the publisher (issue #1041).

The design calls for a GitHub App rather than a personal access token, and the
difference is not bureaucratic. A PAT is a long-lived, account-wide credential
that someone has to create by hand and paste into a secret store. An App holds
a *private key*, and the platform derives short-lived tokens from it:

- an **App JWT**, valid ten minutes, proves "I am this App";
- exchanged for an **installation token**, valid one hour and scoped to the
  repositories that installation actually covers.

So a leak has a bounded life measured in minutes, the blast radius is the
repositories a user deliberately installed on, and nobody has to mint or rotate
a credential by hand. Revocation is uninstalling the App.

Tokens are cached until shortly before they expire, because the exchange is a
network round trip and the publisher runs per job.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from serving.utils.logging import get_logger

logger = get_logger(__name__)

GITHUB_API = "https://api.github.com"

# GitHub rejects an App JWT with more than ten minutes of life. Nine leaves
# room for clock skew without being refused.
_JWT_TTL_S = 540
# Installation tokens last an hour; refresh early so a job never starts with a
# credential that expires mid-push.
_TOKEN_REFRESH_MARGIN_S = 300


class GitHubAppError(Exception):
    """Raised when App credentials cannot be obtained."""


@dataclass(frozen=True)
class AppConfig:
    """The App's identity. The private key never leaves this process."""

    app_id: str
    private_key: str
    api_base: str = GITHUB_API

    @classmethod
    def from_env(cls, env: dict[str, str]) -> AppConfig | None:
        """Build from environment, or None when the App is not configured.

        Returning None rather than raising is deliberate: a deployment without
        the App should simply not publish, not fail to boot.
        """
        app_id = (env.get("AGENT_GITHUB_APP_ID") or "").strip()
        key = env.get("AGENT_GITHUB_APP_PRIVATE_KEY") or ""
        if not key and (path := env.get("AGENT_GITHUB_APP_PRIVATE_KEY_PATH")):
            try:
                with open(path, encoding="utf-8") as handle:
                    key = handle.read()
            except OSError as exc:
                raise GitHubAppError(f"cannot read App private key at {path}: {exc}") from exc
        # A key pasted into an env var usually arrives with escaped newlines;
        # PEM parsing fails on those in a way that is tedious to diagnose.
        key = key.replace("\\n", "\n").strip()
        if not app_id or not key:
            return None
        return cls(app_id=app_id, private_key=key, api_base=env.get("AGENT_GITHUB_API", GITHUB_API))


def build_app_jwt(config: AppConfig, *, now: int | None = None) -> str:
    """Sign the short-lived JWT that proves App identity."""
    issued = int(now if now is not None else time.time())
    try:
        return jwt.encode(
            {
                # Backdated by a minute: GitHub rejects a token whose iat is in
                # the future, and a slightly fast clock is common.
                "iat": issued - 60,
                "exp": issued + _JWT_TTL_S,
                "iss": config.app_id,
            },
            config.private_key,
            algorithm="RS256",
        )
    except Exception as exc:
        raise GitHubAppError(f"could not sign the App JWT: {exc}") from exc


@dataclass
class _CachedToken:
    """One installation's token and when it stops being usable."""

    token: str
    expires_at: float


class GitHubAppCredentials:
    """Mints and caches installation tokens for the publisher."""

    def __init__(self, config: AppConfig, *, timeout_s: float = 30.0) -> None:
        self._config = config
        self._timeout_s = timeout_s
        self._tokens: dict[int, _CachedToken] = {}
        self._installations: dict[str, int] = {}

    async def _request(self, method: str, path: str, *, token: str) -> dict[str, Any]:
        """Call the GitHub API and return the decoded body."""
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            response = await client.request(
                method,
                f"{self._config.api_base}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if response.status_code >= 400:
            # The body can echo the repository name; the token never appears in
            # it, and it is the only useful diagnostic for a misconfigured App.
            raise GitHubAppError(
                f"GitHub {method} {path} failed ({response.status_code}): {response.text[:200]}"
            )
        return response.json()

    async def installation_id_for(self, repo: str) -> int:
        """Return the installation covering ``owner/name``.

        A repository with no installation is not an error to retry — it means
        the user has not installed the App there, and the publisher must say so
        rather than appear to be broken.
        """
        if repo in self._installations:
            return self._installations[repo]
        owner, _, name = repo.partition("/")
        if not owner or not name:
            raise GitHubAppError(f"repo must be 'owner/name', got {repo!r}")
        body = await self._request(
            "GET",
            f"/repos/{owner}/{name}/installation",
            token=build_app_jwt(self._config),
        )
        installation_id = body.get("id")
        if not isinstance(installation_id, int):
            raise GitHubAppError(f"no App installation covers {repo}")
        self._installations[repo] = installation_id
        return installation_id

    async def token_for(self, repo: str) -> str:
        """Return a live installation token for one repository."""
        installation_id = await self.installation_id_for(repo)
        cached = self._tokens.get(installation_id)
        if cached and cached.expires_at - _TOKEN_REFRESH_MARGIN_S > time.time():
            return cached.token

        body = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=build_app_jwt(self._config),
        )
        token = body.get("token")
        if not token:
            raise GitHubAppError("GitHub returned no installation token")
        # GitHub reports expiry; fall back to the documented hour if absent so
        # a missing field cannot produce a token cached forever.
        expires_at = time.time() + 3600
        if raw_expiry := body.get("expires_at"):
            expires_at = _parse_expiry(raw_expiry, default=expires_at)
        self._tokens[installation_id] = _CachedToken(token=token, expires_at=expires_at)
        logger.info(
            "agent_github_app_token_minted",
            extra={
                "event": "agent_github_app_token_minted",
                "repo": repo,
                "installation_id": installation_id,
            },
        )
        return token

    def forget(self, repo: str) -> None:
        """Drop cached state for a repo, e.g. after the App is uninstalled."""
        installation_id = self._installations.pop(repo, None)
        if installation_id is not None:
            self._tokens.pop(installation_id, None)


def _parse_expiry(raw: str, *, default: float) -> float:
    """Parse GitHub's ISO-8601 expiry, falling back on anything unexpected."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return default
