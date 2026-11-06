"""Password hashing and verification utilities using Argon2id.

One Argon2id hash or verification takes 64 MiB and tens of milliseconds of CPU,
and the gateway serves every request from one event loop. Request handlers
therefore use :func:`hash_password_async` and :func:`verify_password_async`,
which run the work on a worker thread, a few at a time; the plain functions are
for command-line tools and tests.
"""

import re
import secrets

import anyio
from anyio.lowlevel import RunVar
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

#: Hashes and verifications that may run at once. Each runs on a worker thread
#: and takes 64 MiB and four lanes; the rest wait without holding a thread, so a
#: burst of sign-ins can neither take every thread the process lends to
#: blocking work nor its memory.
HASH_CONCURRENCY = 2

# One limiter per event loop, the way anyio keeps its own default limiter: a
# limiter made on one loop cannot be waited on from another.
_hash_limiter: RunVar[anyio.CapacityLimiter] = RunVar("password_hash_limiter")

# The hash an account without one is checked against (see
# verify_password_async), made on first use with the parameters above.
_dummy_hash: str | None = None


def hash_password(password: str) -> str:
    """Hash a password using Argon2id.

    Blocks for the length of the hash; a request handler awaits
    :func:`hash_password_async` instead.

    Args:
        password: Plain text password to hash.

    Returns:
        Argon2id hash string.
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash using constant-time comparison.

    Blocks for the length of the verification; a request handler awaits
    :func:`verify_password_async` instead.

    Args:
        plain_password: Plain text password to verify.
        hashed_password: Argon2id hash to compare against.

    Returns:
        True if password matches, False otherwise.
    """
    return pwd_context.verify(plain_password, hashed_password)


def _limiter() -> anyio.CapacityLimiter:
    try:
        return _hash_limiter.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(HASH_CONCURRENCY)
        _hash_limiter.set(limiter)
        return limiter


def _verify_or_spend(plain_password: str, hashed_password: str | None) -> bool:
    """Verify, or with no hash spend the same work on one and answer False."""
    global _dummy_hash
    if hashed_password:
        return verify_password(plain_password, hashed_password)
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(32))
    verify_password(plain_password, _dummy_hash)
    return False


async def hash_password_async(password: str) -> str:
    """:func:`hash_password` on a worker thread, at most :data:`HASH_CONCURRENCY` at once."""
    return await anyio.to_thread.run_sync(hash_password, password, limiter=_limiter())


async def verify_password_async(plain_password: str, hashed_password: str | None) -> bool:
    """:func:`verify_password` on a worker thread, at most :data:`HASH_CONCURRENCY` at once.

    Without *hashed_password* -- no such account, or one that has no password
    -- the password is verified against a stand-in hash and the answer is
    False. The work, and so the time to answer, is the same as for an account
    that has one: a sign-in to a name nobody holds is not told apart by
    answering sooner.
    """
    return await anyio.to_thread.run_sync(
        _verify_or_spend, plain_password, hashed_password, limiter=_limiter()
    )


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
