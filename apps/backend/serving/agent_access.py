"""Deployment-owned Cloud Agent permissions for active gateway accounts."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping
from types import MappingProxyType

AgentAccessPolicy = Callable[[Mapping[str, str]], list[str] | tuple[str, ...]]

logger = logging.getLogger(__name__)

_PERMISSIONS = ("agent.use", "agent.admin")
_policy: AgentAccessPolicy | None = None


class AgentAccessPolicyUnavailable(RuntimeError):
    """The configured policy could not produce a valid permission decision."""


def register_agent_access_policy(policy: AgentAccessPolicy) -> None:
    """Register one synchronous policy from a trusted startup extension.

    The policy receives a read-only mapping containing only ``user_id`` and
    ``role``. Account existence and active status are checked by the gateway
    before it is called. The policy returns explicit permissions; no role
    grants Agent access in the absence of a registered policy.
    """
    global _policy
    if (
        not callable(policy)
        or inspect.iscoroutinefunction(policy)
        or inspect.iscoroutinefunction(policy.__call__)
    ):
        raise TypeError("Agent access policy must be a synchronous callable")
    if _policy is not None:
        raise ValueError("An Agent access policy is already registered")
    _policy = policy


def reset_agent_access_policy() -> None:
    """Clear the process-local policy for isolated tests."""
    global _policy
    _policy = None


def resolve_agent_access_permissions(*, user_id: str, role: str) -> list[str]:
    """Resolve explicit Agent permissions for an already-validated account.

    A missing policy denies access. A policy failure or malformed result is
    unavailable, so callers cannot confuse an operational failure with an
    intentional denial. Decisions are evaluated on every call and not cached.

    Raises:
        AgentAccessPolicyUnavailable: The subject or policy result is invalid,
            or the policy raised an exception.
    """
    try:
        if type(user_id) is not str or not user_id or type(role) is not str or not role:
            raise ValueError("Invalid Agent access subject")
        if _policy is None:
            return []
        result = _policy(MappingProxyType({"user_id": user_id, "role": role}))
        if inspect.iscoroutine(result):
            result.close()
        if type(result) not in (list, tuple) or len(result) > len(_PERMISSIONS):
            raise ValueError("Invalid Agent access permissions")
        if any(
            type(permission) is not str or permission not in _PERMISSIONS for permission in result
        ):
            raise ValueError("Invalid Agent access permission")
        if len(set(result)) != len(result):
            raise ValueError("Duplicate Agent access permissions")
        if "agent.admin" in result and "agent.use" not in result:
            raise ValueError("Agent administration requires Agent use")
        return [permission for permission in _PERMISSIONS if permission in result]
    except Exception:
        logger.warning("Agent access policy evaluation failed.")
        raise AgentAccessPolicyUnavailable("Agent access cannot be resolved.") from None
