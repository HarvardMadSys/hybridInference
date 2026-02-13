"""Unit tests for JWT utilities (PyJWT)."""

import os
from datetime import timedelta

import jwt  # PyJWT, not python-jose
import pytest

from serving.utils.jwt import create_access_token, verify_access_token


@pytest.fixture(scope="module", autouse=True)
def set_jwt_secret():
    """Set JWT_SECRET_KEY for tests."""
    os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-chars-long!!")


class TestJWTCreation:
    """Test access token creation."""

    def test_create_access_token_structure(self):
        """Test create_access_token returns (token, jti) tuple."""
        # CRITICAL: Actual signature is positional args, returns tuple
        token, jti = create_access_token(
            user_id="user_123",
            email="test@example.com",
            tier="free",
        )

        assert isinstance(token, str)
        assert isinstance(jti, str)
        assert jti.startswith("jwt_")  # jti has jwt_ prefix

    def test_access_token_contains_claims(self):
        """Test access token contains expected claims."""
        token, jti = create_access_token(
            user_id="user_123",
            email="test@example.com",
            tier="premium",
        )

        # Decode without verification to check structure
        payload = jwt.decode(token, options={"verify_signature": False})

        assert payload["sub"] == "user_123"
        assert payload["email"] == "test@example.com"
        assert payload["tier"] == "premium"
        assert payload["jti"] == jti
        assert "sid" in payload  # session id
        assert "exp" in payload
        assert "iat" in payload

    def test_create_access_token_custom_expiration(self):
        """Test custom expiration time."""
        token, _ = create_access_token(
            user_id="user_123",
            email="test@example.com",
            expires_delta=timedelta(minutes=1),
        )

        payload = jwt.decode(token, options={"verify_signature": False})

        # Should expire in ~60 seconds
        assert payload["exp"] - payload["iat"] == 60


class TestJWTVerification:
    """Test token verification."""

    def test_verify_valid_token(self):
        """Test verification of valid token."""
        token, jti = create_access_token(
            user_id="user_123",
            email="test@example.com",
        )

        payload = verify_access_token(token)

        assert payload["sub"] == "user_123"
        assert payload["email"] == "test@example.com"
        assert payload["jti"] == jti

    def test_verify_expired_token(self):
        """Test verification of expired token raises jwt.ExpiredSignatureError."""
        import time

        token, _ = create_access_token(
            user_id="user_123",
            email="test@example.com",
            expires_delta=timedelta(seconds=1),
        )

        # Wait for token to expire
        time.sleep(2)

        # CRITICAL: PyJWT raises jwt.ExpiredSignatureError, not JWTError
        with pytest.raises(jwt.ExpiredSignatureError):
            verify_access_token(token)

    def test_verify_invalid_signature(self):
        """Test verification of token with invalid signature raises error."""
        # Create token with wrong secret
        payload = {"sub": "user_123", "email": "test@example.com"}
        token = jwt.encode(payload, "wrong-secret-key", algorithm="HS256")

        # CRITICAL: PyJWT raises jwt.InvalidTokenError (or subclass)
        with pytest.raises(jwt.InvalidTokenError):
            verify_access_token(token)

    def test_verify_malformed_token(self):
        """Test verification of malformed token raises error."""
        with pytest.raises(jwt.InvalidTokenError):
            verify_access_token("not.a.valid.token")
