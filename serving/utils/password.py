"""Password hashing and verification utilities using Argon2id."""

import re

from passlib.context import CryptContext

# Configure Argon2id password context
# Note: passlib's "argon2" scheme uses Argon2id variant by default (since argon2-cffi 18.2.0+)
# This provides the best security against both side-channel and GPU attacks
pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__memory_cost=65536,  # 64 MB
    argon2__time_cost=3,  # 3 iterations
    argon2__parallelism=4,  # 4 threads
    argon2__type="id",  # Explicitly specify Argon2id variant for clarity
)


def hash_password(password: str) -> str:
    """Hash a password using Argon2id.

    Args:
        password: Plain text password to hash.

    Returns:
        Argon2id hash string.
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash using constant-time comparison.

    Args:
        plain_password: Plain text password to verify.
        hashed_password: Argon2id hash to compare against.

    Returns:
        True if password matches, False otherwise.
    """
    return pwd_context.verify(plain_password, hashed_password)


def validate_password_strength(password: str) -> tuple[bool, str | None]:
    """Validate password meets security requirements.

    Requirements:
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one number

    Args:
        password: Password to validate.

    Returns:
        Tuple of (is_valid, error_message).
        If valid, error_message is None.
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"

    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"

    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"

    if not re.search(r"\d", password):
        return False, "Password must contain at least one number"

    return True, None
