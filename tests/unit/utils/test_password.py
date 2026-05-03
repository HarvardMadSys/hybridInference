"""Unit tests for password hashing utilities."""

import os

import pytest

from serving.utils.password import (
    hash_password,
    validate_password_strength,
    verify_password,
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
