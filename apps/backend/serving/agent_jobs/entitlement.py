"""Which repositories a job may target (issue #1041).

The design gives the platform the security boundary and grades credentials by
blast radius. Without a check here both gradings leak in the same way: the job
carries a repository *the requester chose*, and the platform later mints a real
installation token for that string — a read token for the runner to clone with,
and a write token for the publisher to push with. Neither asks whose repository
it is. A user could therefore name any repository the App happens to be
installed on, have the platform clone it into a sandbox, and read the contents
back through their own job's events and patch artifact. That is a confused
deputy: the requester supplies the target, the platform supplies the authority.

P0 is a single-tenant dogfood ("repo == our own"), so the entitlement is an
explicit deployment allowlist rather than a per-user GitHub identity. That is
the honest shape for the stage: it fails closed, it needs no OAuth linkage that
does not exist yet, and it makes the multi-tenant version an obvious next step
(replace :func:`repo_is_allowed` with a per-user installation lookup) instead of
a hole nobody wrote down.

Unset means *nothing* is allowed. A deployment that has not been configured
must refuse jobs, not accept every repository on the internet.
"""

from __future__ import annotations

import os
import re
from typing import Any

# `owner/name`, the only shape GitHub uses and the only one the publisher and
# runner know how to handle. Also keeps a leading `-` out of git's argv.
REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
_REPO_RE = re.compile(REPO_PATTERN)

_ALLOWLIST_VAR = "AGENT_REPO_ALLOWLIST"


class RepoNotAllowed(Exception):
    """Raised when a job names a repository this deployment will not work on."""


def allowed_repos(env: dict[str, str] | None = None) -> list[str]:
    """Return the configured allowlist, lowercased.

    Entries are exact ``owner/name`` values, or ``owner/*`` to allow every
    repository under one owner — enough for "our own org" without becoming a
    pattern language nobody can audit.
    """
    source = env if env is not None else dict(os.environ)
    raw = source.get(_ALLOWLIST_VAR) or ""
    return [entry.strip().lower() for entry in raw.split(",") if entry.strip()]


def repo_is_allowed(repo: str, env: dict[str, str] | None = None) -> bool:
    """Whether ``repo`` is one this deployment is configured to work on."""
    if not _REPO_RE.match(repo or ""):
        return False
    candidate = repo.lower()
    owner = candidate.split("/", 1)[0]
    return any(entry == candidate or entry == f"{owner}/*" for entry in allowed_repos(env))


def require_allowed_repo(repo: str, env: dict[str, str] | None = None) -> None:
    """Raise :class:`RepoNotAllowed` unless ``repo`` is entitled.

    Called both when the job is created and again when a credential is about to
    be minted for it. The second call is not redundant: a row written before
    this check existed, or while the allowlist was wider, must not be able to
    produce a token now.
    """
    if not _REPO_RE.match(repo or ""):
        raise RepoNotAllowed(f"repo must be 'owner/name', got {repo!r}")
    if not repo_is_allowed(repo, env):
        configured = allowed_repos(env)
        detail = (
            f"{_ALLOWLIST_VAR} is not configured, so no repository is allowed"
            if not configured
            else f"{repo!r} is not in {_ALLOWLIST_VAR}"
        )
        raise RepoNotAllowed(f"This deployment will not run agent jobs against {repo!r}: {detail}.")


async def repos_for_user(
    user_id: str,
    *,
    store: Any = None,
    app_credentials: Any = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Every repository this user may target, from both sources of entitlement.

    Two independent ones, and a repository qualifies on either:

    - **A connection the user made.** They authorized the App, GitHub told us
      which installations they can reach, and the installation tells us which
      repositories it covers. This is the design's "user authorizes the repo",
      and it is an entitlement because every step of it is GitHub's answer
      rather than the requester's.
    - **The deployment allowlist.** The single-tenant dogfood, where the repo
      is the operator's own and there is no user to connect.

    Both fail closed: with no connection and no allowlist the answer is empty,
    and an empty answer means no job can be created at all.
    """
    # Only concrete entries seed the list a picker shows; a wildcard is a rule,
    # not a repository, and `require_entitled_repo` matches it separately.
    repos: set[str] = {entry for entry in allowed_repos(env) if not entry.endswith("/*")}
    if store is None or app_credentials is None:
        return sorted(repos)
    try:
        grants = await store.list_repo_grants(user_id=user_id)
    except Exception:
        return sorted(repos)
    for grant in grants:
        try:
            covered = await app_credentials.repositories_for_installation(grant["installation_id"])
        except Exception:
            # One unreachable installation must not hide the others, and must
            # not widen anything either — it simply contributes nothing.
            continue
        repos.update(name for name in covered if _REPO_RE.match(name or ""))
    return sorted(repos)


async def require_entitled_repo(
    repo: str,
    user_id: str,
    *,
    store: Any = None,
    app_credentials: Any = None,
    env: dict[str, str] | None = None,
) -> None:
    """Raise :class:`RepoNotAllowed` unless this user may target ``repo``."""
    if not _REPO_RE.match(repo or ""):
        raise RepoNotAllowed(f"repo must be 'owner/name', got {repo!r}")
    # The allowlist half first, so an `owner/*` rule is honoured without needing
    # to enumerate every repository under that owner.
    if repo_is_allowed(repo, env):
        return
    entitled = await repos_for_user(user_id, store=store, app_credentials=app_credentials, env=env)
    if not any(entry.lower() == repo.lower() for entry in entitled):
        raise RepoNotAllowed(
            f"You have not connected {repo!r}. Connect the GitHub App on that "
            "repository, or ask an operator to add it to this deployment's allowlist."
        )


__all__ = [
    "REPO_PATTERN",
    "RepoNotAllowed",
    "allowed_repos",
    "repo_is_allowed",
    "repos_for_user",
    "require_allowed_repo",
    "require_entitled_repo",
]
