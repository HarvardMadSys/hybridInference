"""Behavioral tests for DualWriteOperationalStore and DualWriteLogStore.

Uses AsyncMock stores to verify:
- Reads only hit primary (shadow never called)
- Writes hit both stores (primary first)
- Shadow failure does not propagate
- Primary failure propagates (shadow not called)
- health_check returns primary health; shadow_healthy tracks shadow
- Argument passthrough fidelity for writes with optional kwargs

Shadow writes now run as fire-and-forget tracked_tasks. The
``_drain_shadow_tasks`` helper awaits any pending tracked tasks before
assertions so existing behavioural tests remain deterministic.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest

from serving.observability.tracked_tasks import _TRACKED_TASKS
from serving.storage.base import LogStore, OperationalStore
from serving.storage.dual_write import DualWriteLogStore, DualWriteOperationalStore


async def _drain_shadow_tasks() -> None:
    """Await any pending tracked_task shadow writes scheduled by dual_write."""
    if _TRACKED_TASKS:
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)


@pytest.fixture(autouse=True)
def _clear_tracked_tasks_between_tests():
    """Ensure the module-level tracked-task set is empty for each test."""
    _TRACKED_TASKS.clear()
    yield
    _TRACKED_TASKS.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_abstract_methods(abc_class: type) -> set[str]:
    return {
        name
        for name, _ in inspect.getmembers(abc_class, predicate=inspect.isfunction)
        if getattr(getattr(abc_class, name), "__isabstractmethod__", False)
    }


# Lifecycle + health are special; not pure reads or writes.
_LIFECYCLE = {"initialize", "cleanup", "health_check"}

# OperationalStore write methods (mutate state).
_OP_WRITES = {
    "create_user",
    "update_user_fields",
    "update_user_last_login",
    "delete_user",
    "resume_user",
    "hard_delete_user",
    "approve_user",
    "reject_user",
    "create_key",
    "update_key_last_used",
    "update_key",
    "revoke_key",
    "regenerate_key",
    "create_session",
    "rotate_session",
    "revoke_session",
    "delete_user_sessions",
    "create_verification_token",
    "mark_verification_used",
    "mark_user_email_verified",
    "delete_user_verification_tokens",
    "create_reset_token",
    "mark_reset_used",
    "delete_user_reset_tokens",
    "log_admin_action",
    "increment_user_cost",
    "update_user_preferences",
    "add_signup_allowed_domain",
    "remove_signup_allowed_domain",
    "set_setting",
}

_OP_READS = _get_abstract_methods(OperationalStore) - _OP_WRITES - _LIFECYCLE

# LogStore writes (mutate state).
_LOG_WRITES = {"log_request", "hard_delete_user_data"}
_LOG_READS = _get_abstract_methods(LogStore) - _LOG_WRITES - _LIFECYCLE


def _mock_op_store() -> AsyncMock:
    """Create an AsyncMock that passes isinstance checks for OperationalStore."""
    mock = AsyncMock(spec=OperationalStore)
    mock.regenerate_key.return_value = "old_prefix"
    mock.create_key.return_value = {"id": 1, "created_at": "2024-01-01"}
    mock.health_check.return_value = True
    mock.list_users.return_value = (0, [], {})
    mock.list_keys.return_value = (0, [])
    mock.list_audit_log.return_value = (0, [])
    mock.get_user_cost_today.return_value = 0.0
    mock.get_user_cost_period.return_value = 0.0
    mock.get_batch_usage.return_value = {}
    mock.get_user_preferences.return_value = {}
    mock.get_user_counts_by_status.return_value = {}
    mock.get_active_user_counts.return_value = {}
    mock.check_active_key_exists.return_value = False
    return mock


def _mock_log_store() -> AsyncMock:
    mock = AsyncMock(spec=LogStore)
    mock.health_check.return_value = True
    mock.get_user_cost_today.return_value = 0.0
    mock.get_user_cost_period.return_value = 0.0
    mock.get_batch_usage.return_value = {}
    mock.get_user_usage_detail.return_value = {}
    mock.get_user_detail_usage.return_value = {}
    mock.get_key_detail_usage.return_value = {}
    mock.get_model_activity.return_value = {}
    mock.get_stats.return_value = []
    return mock


# ---------------------------------------------------------------------------
# DualWriteOperationalStore
# ---------------------------------------------------------------------------


class TestDualWriteOperationalReads:
    """Read methods must only call primary, never shadow."""

    @pytest.mark.asyncio
    async def test_reads_do_not_touch_shadow(self):
        primary = _mock_op_store()
        shadow = _mock_op_store()
        store = DualWriteOperationalStore(primary, shadow)

        # Call every read method with minimal args
        for method_name in _OP_READS:
            primary_method = getattr(primary, method_name)
            shadow_method = getattr(shadow, method_name)

            # Build minimal positional args from the signature
            sig = inspect.signature(getattr(OperationalStore, method_name))
            params = list(sig.parameters.values())[1:]  # skip self
            args = []
            kwargs = {}
            for p in params:
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                    if p.default is p.empty:
                        args.append("dummy")
                elif p.kind == p.KEYWORD_ONLY and p.default is p.empty:
                    kwargs[p.name] = "dummy"

            await getattr(store, method_name)(*args, **kwargs)
            assert primary_method.called, f"{method_name} did not call primary"
            assert not shadow_method.called, f"{method_name} unexpectedly called shadow"


class TestDualWriteOperationalWrites:
    """Write methods must call primary then shadow."""

    @pytest.mark.asyncio
    async def test_create_user_calls_both(self):
        primary = _mock_op_store()
        shadow = _mock_op_store()
        store = DualWriteOperationalStore(primary, shadow)

        await store.create_user(
            user_id="u1",
            email="a@b.com",
            password_hash="hash",
            user_name="Test",
            email_verified=True,
            status="active",
        )
        await _drain_shadow_tasks()
        primary.create_user.assert_awaited_once()
        shadow.create_user.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shadow_failure_does_not_propagate(self):
        primary = _mock_op_store()
        shadow = _mock_op_store()
        shadow.create_user.side_effect = RuntimeError("pg down")
        store = DualWriteOperationalStore(primary, shadow)

        # Should not raise
        await store.create_user(
            user_id="u1",
            email="a@b.com",
            password_hash="hash",
        )
        await _drain_shadow_tasks()
        primary.create_user.assert_awaited_once()
        assert not store.shadow_healthy

    @pytest.mark.asyncio
    async def test_primary_failure_propagates_shadow_not_called(self):
        primary = _mock_op_store()
        shadow = _mock_op_store()
        primary.update_user_fields.side_effect = RuntimeError("d1 down")
        store = DualWriteOperationalStore(primary, shadow)

        with pytest.raises(RuntimeError, match="d1 down"):
            await store.update_user_fields("u1", role="admin")
        shadow.update_user_fields.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_regenerate_key_returns_primary_result(self):
        primary = _mock_op_store()
        shadow = _mock_op_store()
        primary.regenerate_key.return_value = "old_pfx"
        store = DualWriteOperationalStore(primary, shadow)

        result = await store.regenerate_key("u1", new_key_hash="hash", new_key_prefix="new_pfx")
        await _drain_shadow_tasks()
        assert result == "old_pfx"
        shadow.regenerate_key.assert_awaited_once()


class TestDualWriteOperationalArgPassthrough:
    """Verify optional kwargs are faithfully passed through to both stores."""

    @pytest.mark.asyncio
    async def test_delete_user_passes_all_optional_kwargs(self):
        """delete_user has optional reason and email — verify both land on shadow."""
        primary = _mock_op_store()
        shadow = _mock_op_store()
        store = DualWriteOperationalStore(primary, shadow)

        await store.delete_user(
            "u1",
            admin_ip="1.2.3.4",
            admin_id="admin",
            reason="compliance",
            email="user@test.com",
        )
        await _drain_shadow_tasks()
        # Verify shadow received the same kwargs
        shadow.delete_user.assert_awaited_once_with(
            "u1",
            admin_ip="1.2.3.4",
            admin_id="admin",
            reason="compliance",
            email="user@test.com",
        )

    @pytest.mark.asyncio
    async def test_create_key_passes_all_optional_kwargs(self):
        """create_key has many optional params — verify they all pass through."""
        primary = _mock_op_store()
        shadow = _mock_op_store()
        store = DualWriteOperationalStore(primary, shadow)

        await store.create_key(
            key_hash="h",
            key_prefix="pfx",
            user_id="u1",
            user_name="Test",
            quota_daily_cost_usd=500.0,
            quota_monthly_cost_usd=5000.0,
            expires_at=None,
            notes="test key",
            metadata='{"foo": 1}',
            account_id="acc1",
        )
        await _drain_shadow_tasks()
        shadow.create_key.assert_awaited_once_with(
            key_hash="h",
            key_prefix="pfx",
            user_id="u1",
            user_name="Test",
            quota_daily_cost_usd=500.0,
            quota_monthly_cost_usd=5000.0,
            expires_at=None,
            notes="test key",
            metadata='{"foo": 1}',
            account_id="acc1",
        )

    @pytest.mark.asyncio
    async def test_log_admin_action_passes_optional_kwargs(self):
        """log_admin_action has optional target_user_id and details."""
        primary = _mock_op_store()
        shadow = _mock_op_store()
        store = DualWriteOperationalStore(primary, shadow)

        await store.log_admin_action(
            admin_ip="1.2.3.4",
            action="delete_user",
            target_user_id="u1",
            details={"reason": "spam"},
            success=False,
        )
        await _drain_shadow_tasks()
        shadow.log_admin_action.assert_awaited_once_with(
            admin_ip="1.2.3.4",
            action="delete_user",
            target_user_id="u1",
            details={"reason": "spam"},
            success=False,
        )


class TestDualWriteOperationalHealth:
    """health_check returns primary health; shadow_healthy tracks shadow."""

    @pytest.mark.asyncio
    async def test_primary_healthy_shadow_healthy(self):
        primary = _mock_op_store()
        shadow = _mock_op_store()
        store = DualWriteOperationalStore(primary, shadow)

        assert await store.health_check() is True
        assert store.shadow_healthy is True

    @pytest.mark.asyncio
    async def test_primary_unhealthy(self):
        primary = _mock_op_store()
        shadow = _mock_op_store()
        primary.health_check.return_value = False
        store = DualWriteOperationalStore(primary, shadow)

        assert await store.health_check() is False

    @pytest.mark.asyncio
    async def test_shadow_unhealthy_still_returns_true(self):
        primary = _mock_op_store()
        shadow = _mock_op_store()
        shadow.health_check.return_value = False
        store = DualWriteOperationalStore(primary, shadow)

        assert await store.health_check() is True
        assert store.shadow_healthy is False

    @pytest.mark.asyncio
    async def test_shadow_health_exception_still_returns_primary(self):
        primary = _mock_op_store()
        shadow = _mock_op_store()
        shadow.health_check.side_effect = RuntimeError("pg down")
        store = DualWriteOperationalStore(primary, shadow)

        assert await store.health_check() is True
        assert store.shadow_healthy is False


class TestDualWriteOperationalLifecycle:
    """initialize and cleanup call both stores."""

    @pytest.mark.asyncio
    async def test_initialize_calls_both(self):
        primary = _mock_op_store()
        shadow = _mock_op_store()
        store = DualWriteOperationalStore(primary, shadow)

        await store.initialize()
        primary.initialize.assert_awaited_once()
        shadow.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cleanup_calls_both(self):
        primary = _mock_op_store()
        shadow = _mock_op_store()
        store = DualWriteOperationalStore(primary, shadow)

        await store.cleanup()
        primary.cleanup.assert_awaited_once()
        shadow.cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shadow_init_failure_does_not_propagate(self):
        primary = _mock_op_store()
        shadow = _mock_op_store()
        shadow.initialize.side_effect = RuntimeError("pg down")
        store = DualWriteOperationalStore(primary, shadow)

        await store.initialize()  # should not raise
        assert not store.shadow_healthy


# ---------------------------------------------------------------------------
# DualWriteLogStore
# ---------------------------------------------------------------------------


class TestDualWriteLogReads:
    """Read methods must only call primary, never shadow."""

    @pytest.mark.asyncio
    async def test_reads_do_not_touch_shadow(self):
        primary = _mock_log_store()
        shadow = _mock_log_store()
        store = DualWriteLogStore(primary, shadow)

        for method_name in _LOG_READS:
            primary_method = getattr(primary, method_name)
            shadow_method = getattr(shadow, method_name)

            sig = inspect.signature(getattr(LogStore, method_name))
            params = list(sig.parameters.values())[1:]
            args = []
            kwargs = {}
            for p in params:
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                    if p.default is p.empty:
                        args.append("dummy")
                elif p.kind == p.KEYWORD_ONLY and p.default is p.empty:
                    kwargs[p.name] = "dummy"

            await getattr(store, method_name)(*args, **kwargs)
            assert primary_method.called, f"{method_name} did not call primary"
            assert not shadow_method.called, f"{method_name} unexpectedly called shadow"


class TestDualWriteLogWrites:
    """log_request must call both stores."""

    @pytest.mark.asyncio
    async def test_log_request_calls_both(self):
        primary = _mock_log_store()
        shadow = _mock_log_store()
        store = DualWriteLogStore(primary, shadow)

        await store.log_request(
            request_id="r1",
            model_id="m1",
            provider="p1",
            prompt="hello",
            response=None,
            usage=None,
            latency_ms=100,
            status_code=200,
        )
        await _drain_shadow_tasks()
        primary.log_request.assert_awaited_once()
        shadow.log_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shadow_failure_does_not_propagate(self):
        primary = _mock_log_store()
        shadow = _mock_log_store()
        shadow.log_request.side_effect = RuntimeError("pg down")
        store = DualWriteLogStore(primary, shadow)

        await store.log_request(
            request_id="r1",
            model_id="m1",
            provider="p1",
            prompt="hello",
            response=None,
            usage=None,
            latency_ms=100,
            status_code=200,
        )
        await _drain_shadow_tasks()
        primary.log_request.assert_awaited_once()
        assert not store.shadow_healthy

    @pytest.mark.asyncio
    async def test_primary_failure_propagates(self):
        primary = _mock_log_store()
        shadow = _mock_log_store()
        primary.log_request.side_effect = RuntimeError("d1 down")
        store = DualWriteLogStore(primary, shadow)

        with pytest.raises(RuntimeError, match="d1 down"):
            await store.log_request(
                request_id="r1",
                model_id="m1",
                provider="p1",
                prompt="hello",
                response=None,
                usage=None,
                latency_ms=100,
                status_code=200,
            )
        shadow.log_request.assert_not_awaited()


class TestDualWriteLogArgPassthrough:
    """Verify all optional kwargs pass through to shadow."""

    @pytest.mark.asyncio
    async def test_log_request_passes_all_optional_kwargs(self):
        primary = _mock_log_store()
        shadow = _mock_log_store()
        store = DualWriteLogStore(primary, shadow)

        await store.log_request(
            request_id="r1",
            model_id="m1",
            provider="p1",
            prompt=[{"role": "user", "content": "hi"}],
            response={"choices": []},
            usage={"prompt_tokens": 5},
            latency_ms=100,
            status_code=200,
            error=None,
            params={"temperature": 0.7},
            metadata={"user_id": "u1"},
            ttft_ms=50,
            prompt_hash="ph",
            response_hash="rh",
            store_full_content=True,
            pricing={"model": "0.01"},
        )
        await _drain_shadow_tasks()
        shadow.log_request.assert_awaited_once_with(
            request_id="r1",
            model_id="m1",
            provider="p1",
            prompt=[{"role": "user", "content": "hi"}],
            response={"choices": []},
            usage={"prompt_tokens": 5},
            latency_ms=100,
            status_code=200,
            error=None,
            params={"temperature": 0.7},
            metadata={"user_id": "u1"},
            ttft_ms=50,
            prompt_hash="ph",
            response_hash="rh",
            store_full_content=True,
            pricing={"model": "0.01"},
            upstream_cost_usd=None,
        )


class TestDualWriteLogHealth:
    @pytest.mark.asyncio
    async def test_primary_healthy_shadow_healthy(self):
        primary = _mock_log_store()
        shadow = _mock_log_store()
        store = DualWriteLogStore(primary, shadow)

        assert await store.health_check() is True
        assert store.shadow_healthy is True

    @pytest.mark.asyncio
    async def test_shadow_unhealthy_still_returns_true(self):
        primary = _mock_log_store()
        shadow = _mock_log_store()
        shadow.health_check.return_value = False
        store = DualWriteLogStore(primary, shadow)

        assert await store.health_check() is True
        assert store.shadow_healthy is False
