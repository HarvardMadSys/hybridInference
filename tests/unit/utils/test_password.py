"""Unit tests for password hashing utilities."""

import asyncio
import os
import threading

import pytest

from serving.utils import password as password_module
from serving.utils.password import (
    HASH_CONCURRENCY,
    HASH_MAX_WAITERS,
    PasswordWorkBusyError,
    hash_password,
    hash_password_async,
    validate_password_strength,
    verify_password,
    verify_password_async,
)


class TestPasswordHashing:
    """Test password hashing and verification."""

    def test_hash_password_returns_different_hash_each_time(self):
        """Test that same password produces different hashes (salt)."""
        password = "TestPass123!"
        hash1 = hash_password(password)
        hash2 = hash_password(password)

        assert hash1 != hash2
        assert hash1.startswith("$argon2")
        assert hash2.startswith("$argon2")

    def test_verify_password_correct(self):
        """Test password verification with correct password."""
        password = "TestPass123!"
        hashed = hash_password(password)

        assert verify_password(password, hashed) is True

    def test_verify_password_incorrect(self):
        """Test password verification with incorrect password."""
        password = "TestPass123!"
        hashed = hash_password(password)

        assert verify_password("WrongPassword!", hashed) is False

    def test_hash_password_uses_argon2(self):
        """Test that hash uses Argon2 algorithm."""
        password = "TestPass123!"
        hashed = hash_password(password)

        assert hashed.startswith("$argon2")


class TestPasswordValidation:
    """Test password strength validation."""

    def test_validate_strong_password(self):
        """Test that strong password passes validation."""
        is_valid, error = validate_password_strength("SecurePass123!")

        assert is_valid is True
        assert error is None

    def test_validate_password_too_short(self):
        """Test that password too short fails validation."""
        is_valid, error = validate_password_strength("Short1!")

        assert is_valid is False
        assert "8 characters" in error

    def test_validate_password_no_uppercase(self):
        """Test that password without uppercase fails validation."""
        is_valid, error = validate_password_strength("securepass123!")

        assert is_valid is False
        assert "uppercase" in error.lower()

    def test_validate_password_no_lowercase(self):
        """Test that password without lowercase fails validation."""
        is_valid, error = validate_password_strength("SECUREPASS123!")

        assert is_valid is False
        assert "lowercase" in error.lower()

    def test_validate_password_no_number(self):
        """Test that password without number fails validation."""
        is_valid, error = validate_password_strength("SecurePassword!")

        assert is_valid is False
        assert "number" in error.lower()


async def _until(condition, timeout: float = 5.0) -> None:
    """Yield to the loop until *condition* holds; the loop must stay free meanwhile."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition never held"
        await asyncio.sleep(0.005)


class TestHashingOffTheEventLoop:
    """Request handlers hash and verify on worker threads, a few at a time."""

    async def test_hashing_runs_on_a_worker_thread(self, monkeypatch):
        threads = []

        def recording_hash(password):
            threads.append(threading.get_ident())
            return f"hashed:{password}"

        monkeypatch.setattr(password_module, "hash_password", recording_hash)

        assert await hash_password_async("pw") == "hashed:pw"
        assert threads and threads[0] != threading.get_ident()

    async def test_the_loop_keeps_serving_while_a_hash_runs(self, monkeypatch):
        started, release = threading.Event(), threading.Event()

        def blocking_hash(password):
            started.set()
            # Run on the loop, this would hold it until the timeout and the
            # coroutine below could never run first.
            release.wait(5)
            return "hashed"

        monkeypatch.setattr(password_module, "hash_password", blocking_hash)
        hashing = asyncio.create_task(hash_password_async("pw"))
        await _until(started.is_set)

        served = await asyncio.wait_for(asyncio.sleep(0, result="served"), timeout=1)

        assert served == "served"
        assert not hashing.done()
        release.set()
        assert await hashing == "hashed"

    async def test_at_most_hash_concurrency_run_at_once(self, monkeypatch):
        lock = threading.Lock()
        release = threading.Event()
        running = peak = 0

        def counted(password):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            release.wait(5)
            with lock:
                running -= 1
            return password

        def now_running():
            with lock:
                return running

        monkeypatch.setattr(
            password_module, "verify_password", lambda plain, _: bool(counted(plain))
        )
        calls = HASH_CONCURRENCY * 3
        tasks = [
            asyncio.create_task(verify_password_async(f"pw{i}", "stored")) for i in range(calls)
        ]
        await _until(lambda: now_running() == HASH_CONCURRENCY)
        # The others wait for a slot; give them every chance to take one.
        await asyncio.sleep(0.1)
        assert now_running() == HASH_CONCURRENCY

        release.set()
        assert await asyncio.gather(*tasks) == [True] * calls
        assert peak == HASH_CONCURRENCY

    async def test_verify_async_answers_like_verify(self):
        hashed = hash_password("TestPass123!")

        assert await verify_password_async("TestPass123!", hashed) is True
        assert await verify_password_async("WrongPassword!", hashed) is False
        assert await hash_password_async("TestPass123!") != hashed

    @pytest.mark.parametrize("missing", [None, ""])
    async def test_no_hash_still_spends_a_verification_and_answers_false(
        self, monkeypatch, missing
    ):
        verified = []

        def spy(plain, hashed):
            verified.append(hashed)
            return True

        monkeypatch.setattr(password_module, "verify_password", spy)

        assert await verify_password_async("anything", missing) is False
        # The same work as an account with a password: one Argon2id
        # verification, against a hash with the same parameters.
        assert len(verified) == 1
        assert verified[0].startswith("$argon2id$v=19$m=65536,t=3,p=4$")


class _Blocking:
    """A verification that holds its slot until released, counting how many run."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.running = 0

    def __call__(self, plain, hashed):
        with self._lock:
            self.running += 1
        self.release.wait(5)
        with self._lock:
            self.running -= 1
        return True

    def now_running(self) -> int:
        with self._lock:
            return self.running


def _waiting() -> int:
    return password_module._limiter().statistics().tasks_waiting


class TestBoundedWaiting:
    """A flood of checks is refused at once past a bound, not queued without one."""

    async def test_past_the_bound_password_work_is_refused_at_once(self, monkeypatch):
        blocking = _Blocking()
        monkeypatch.setattr(password_module, "verify_password", blocking)
        held = [
            asyncio.create_task(verify_password_async(f"pw{i}", "stored"))
            for i in range(HASH_CONCURRENCY + HASH_MAX_WAITERS)
        ]
        await _until(lambda: blocking.now_running() == HASH_CONCURRENCY)
        await _until(lambda: _waiting() == HASH_MAX_WAITERS)

        # The same answer whether the name has an account or not, and for a hash.
        for hashed in ("stored", None):
            with pytest.raises(PasswordWorkBusyError):
                await asyncio.wait_for(verify_password_async("guess", hashed), timeout=1)
        with pytest.raises(PasswordWorkBusyError):
            await asyncio.wait_for(hash_password_async("NewPass123"), timeout=1)
        assert _waiting() == HASH_MAX_WAITERS

        # A signed-in session's check is not refused: it waits its turn.
        owner = asyncio.create_task(verify_password_async("owner", "stored", shed=False))
        await _until(lambda: _waiting() == HASH_MAX_WAITERS + 1)

        blocking.release.set()
        assert await owner is True
        assert await asyncio.gather(*held) == [True] * len(held)
        # Drained: work is admitted again.
        password_module.ensure_password_capacity()

    async def test_under_the_bound_nothing_is_refused(self, monkeypatch):
        monkeypatch.setattr(password_module, "verify_password", lambda plain, hashed: True)

        results = await asyncio.gather(
            *(verify_password_async(f"pw{i}", "stored") for i in range(HASH_MAX_WAITERS))
        )

        assert results == [True] * HASH_MAX_WAITERS


class TestStandInBudget:
    """Checks for names nobody holds spend Argon2id only within a process-wide budget."""

    async def test_past_the_budget_a_stand_in_check_waits_like_one_without_the_work(
        self, monkeypatch
    ):
        verified = []
        monkeypatch.setattr(
            password_module, "verify_password", lambda plain, hashed: verified.append(hashed)
        )
        monkeypatch.setattr(
            password_module, "_stand_in_budget", password_module._StandInBudget(0, 0)
        )
        password_module._recent_check_seconds.extend([0.05, 0.05])

        started = asyncio.get_running_loop().time()
        assert await verify_password_async("guess", None) is False
        elapsed = asyncio.get_running_loop().time() - started

        assert verified == []
        # As long as a recent real check took.
        assert 0.04 <= elapsed < 1.0

    async def test_an_account_with_a_password_is_always_checked(self, monkeypatch):
        verified = []

        def spy(plain, hashed):
            verified.append(hashed)
            return False

        monkeypatch.setattr(password_module, "verify_password", spy)
        monkeypatch.setattr(
            password_module, "_stand_in_budget", password_module._StandInBudget(0, 0)
        )

        assert await verify_password_async("guess", "stored") is False
        assert verified == ["stored"]

    def test_the_budget_refills_at_its_rate_up_to_its_burst(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(password_module.time, "monotonic", lambda: clock[0])
        budget = password_module._StandInBudget(per_sec=2.0, burst=3)

        assert [budget.take() for _ in range(4)] == [True, True, True, False]
        clock[0] += 0.5  # one token
        assert [budget.take() for _ in range(2)] == [True, False]
        clock[0] += 100  # never more than the burst
        assert [budget.take() for _ in range(4)] == [True, True, True, False]

    def test_the_budget_is_a_small_share_of_the_hashing_capacity(self):
        # At ~60 ms a check, HASH_CONCURRENCY slots make ~33 checks a second.
        assert password_module.STAND_IN_CHECKS_PER_SEC * 0.06 <= HASH_CONCURRENCY / 4


# Performance test (skip by default)
skip_if_not_perf = pytest.mark.skipif(
    os.getenv("RUN_PERF") != "1",
    reason="Performance tests are disabled by default (set RUN_PERF=1 to enable)",
)


@skip_if_not_perf
@pytest.mark.perf
class TestPasswordPerformance:
    """Test password hashing performance (skip by default)."""

    def test_password_hashing_performance(self):
        """Test password hashing completes in reasonable time.

        Note: Argon2id is intentionally slow (64MB, time_cost=3, parallelism=4).
        This test may fail on slow CI machines. Run with RUN_PERF=1 to enable.
        """
        import time

        password = "SecurePass123!"
        iterations = 10

        start = time.time()
        for _ in range(iterations):
            hash_password(password)
        elapsed = time.time() - start

        # Should complete 10 hashes in under 10 seconds (generous for CI)
        assert elapsed < 10.0, f"10 hashes took {elapsed:.2f}s (expected < 10s)"
