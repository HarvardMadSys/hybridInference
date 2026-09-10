"""Agent permissions are explicit, deployment-owned and fail closed."""

from __future__ import annotations

import inspect
from unittest.mock import Mock

import pytest

from serving import agent_access


@pytest.fixture(autouse=True)
def isolated_policy():
    agent_access.reset_agent_access_policy()
    yield
    agent_access.reset_agent_access_policy()


def _resolve(role="pro"):
    return agent_access.resolve_agent_access_permissions(user_id="user_1", role=role)


@pytest.mark.parametrize("role", ["free", "pro", "internal", "admin"])
def test_no_role_has_agent_access_without_a_policy(role):
    assert _resolve(role) == []


def test_policy_receives_a_minimal_read_only_subject():
    def policy(subject):
        assert dict(subject) == {"user_id": "user_1", "role": "pro"}
        with pytest.raises(TypeError):
            subject["role"] = "admin"
        return ["agent.use"]

    agent_access.register_agent_access_policy(policy)
    assert _resolve() == ["agent.use"]


@pytest.mark.parametrize(
    ("result", "permissions"),
    [
        ([], []),
        ((), []),
        (["agent.use"], ["agent.use"]),
        (("agent.use",), ["agent.use"]),
        (["agent.use", "agent.admin"], ["agent.use", "agent.admin"]),
        (("agent.admin", "agent.use"), ["agent.use", "agent.admin"]),
    ],
)
def test_valid_permissions_are_returned_in_canonical_order(result, permissions):
    agent_access.register_agent_access_policy(lambda _: result)
    assert _resolve() == permissions


@pytest.mark.parametrize(
    "result",
    [
        None,
        True,
        "agent.use",
        {"allowed": True, "permissions": ["agent.use"]},
        {"agent.use"},
        [True],
        [1],
        [[]],
        ["agent.use", "agent.use"],
        ["agent.admin"],
        ["agent.use", "agent.admin", "agent.other"],
        ["agent.other"],
        ["Agent.Use"],
        ["agent.use "],
    ],
)
def test_invalid_policy_results_are_unavailable(result):
    agent_access.register_agent_access_policy(lambda _: result)
    with pytest.raises(agent_access.AgentAccessPolicyUnavailable):
        _resolve()


def test_permission_string_subclasses_are_not_coerced():
    class Permission(str):
        pass

    agent_access.register_agent_access_policy(lambda _: [Permission("agent.use")])
    with pytest.raises(agent_access.AgentAccessPolicyUnavailable):
        _resolve()


def test_policy_exceptions_do_not_expose_their_details(caplog):
    policy = Mock(side_effect=RuntimeError("private policy detail"))
    agent_access.register_agent_access_policy(policy)

    with pytest.raises(agent_access.AgentAccessPolicyUnavailable) as exc:
        _resolve()

    assert "private policy detail" not in str(exc.value)
    assert "private policy detail" not in caplog.text
    assert "Agent access policy evaluation failed" in caplog.text


@pytest.mark.parametrize("policy", [None, 7, "deployment.policy"])
def test_registration_rejects_non_callables(policy):
    with pytest.raises(TypeError, match="synchronous callable"):
        agent_access.register_agent_access_policy(policy)


def test_registration_rejects_async_functions_and_callable_objects():
    async def policy(_):
        return ["agent.use"]

    class AsyncPolicy:
        async def __call__(self, _):
            return ["agent.use"]

    for candidate in (policy, AsyncPolicy()):
        with pytest.raises(TypeError, match="synchronous callable"):
            agent_access.register_agent_access_policy(candidate)


def test_wrapped_async_policy_fails_closed_and_closes_the_coroutine():
    async def policy(_):
        return ["agent.use"]

    pending = policy(None)
    agent_access.register_agent_access_policy(lambda _: pending)
    with pytest.raises(agent_access.AgentAccessPolicyUnavailable):
        _resolve()
    assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED


def test_a_second_registration_cannot_replace_the_policy():
    agent_access.register_agent_access_policy(lambda _: ["agent.use"])
    with pytest.raises(ValueError, match="already registered"):
        agent_access.register_agent_access_policy(lambda _: ["agent.use", "agent.admin"])
    assert _resolve() == ["agent.use"]


def test_decisions_are_not_cached_and_reset_restores_default_denial():
    result = ["agent.use"]
    agent_access.register_agent_access_policy(lambda _: result)
    assert _resolve() == ["agent.use"]
    result.clear()
    assert _resolve() == []
    agent_access.reset_agent_access_policy()
    assert _resolve() == []
