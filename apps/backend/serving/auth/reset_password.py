"""Reset an account's password from the command line.

Usage::

    python -m serving.auth.reset_password <login-name-or-email> [--generate]

This is how the administrator created by first-run setup recovers its
account: it has no email address, so there is no reset link to send. It works
for any account. Run it where the backend runs, for example in the standard
container::

    docker exec -it hybridinference-backend python -m serving.auth.reset_password admin

It reads only the database connection settings (``DB_HOST``, ``DB_PORT``,
``DB_NAME``, ``DB_USER``, ``DB_PASSWORD``), prompts twice for the new password
or, with ``--generate``, creates and prints a strong one, stores its Argon2
hash, and revokes the account's sessions so every browser must sign in again.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import string
import sys
from typing import TYPE_CHECKING, Any

from serving.utils import password as password_utils

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_GENERATED_PASSWORD_LENGTH = 20
_GENERATED_PASSWORD_ALPHABET = string.ascii_letters + string.digits


class ResetPasswordError(Exception):
    """The reset did not happen; the message tells the operator why and what to do."""


def generate_password() -> str:
    """Return a random password that meets the signup strength rules."""
    while True:
        candidate = "".join(
            secrets.choice(_GENERATED_PASSWORD_ALPHABET) for _ in range(_GENERATED_PASSWORD_LENGTH)
        )
        if password_utils.validate_password_strength(candidate)[0]:
            return candidate


def check_password(new_password: str) -> None:
    """Apply the signup strength rules.

    Raises:
        ResetPasswordError: If the password is too weak.
    """
    is_valid, error_msg = password_utils.validate_password_strength(new_password)
    if not is_valid:
        reason = error_msg or "Password does not meet security requirements"
        raise ResetPasswordError(f"{reason}. Nothing was changed.")


def prompt_new_password(identifier: str, read_password: Callable[[str], str] | None = None) -> str:
    """Ask twice for the new password, without echoing it, and check it.

    Args:
        identifier: The account, named in the prompt.
        read_password: Reads one password; ``getpass.getpass`` by default.

    Raises:
        ResetPasswordError: If the two entries differ or the password is weak.
    """
    read = read_password or getpass.getpass
    new_password = read(f"New password for {identifier}: ")
    if read("Repeat the new password: ") != new_password:
        raise ResetPasswordError("The passwords do not match. Nothing was changed.")
    check_password(new_password)
    return new_password


async def reset_password(store: Any, identifier: str, new_password: str) -> dict[str, Any]:
    """Set the password of the account *identifier* names and revoke its sessions.

    Args:
        store: Operational store holding the account.
        identifier: The account's email address or, without an ``@``, its
            login name.
        new_password: The new password; it must meet the strength rules.

    Returns:
        The account's user row, as it was before the reset.

    Raises:
        ResetPasswordError: If the password is weak or no account matches.
    """
    check_password(new_password)
    identifier = identifier.strip()
    if "@" in identifier:
        account = await store.get_user_by_email(identifier)
    else:
        account = await store.get_user_by_login_name(identifier)
    if account is None:
        raise ResetPasswordError(
            f"No account has the login name or email address {identifier!r}. Nothing was changed."
        )

    await store.update_user_fields(
        account["id"], password_hash=password_utils.hash_password(new_password)
    )
    await store.delete_user_sessions(account["id"])
    return account


def describe_reset(account: dict[str, Any], generated_password: str | None) -> list[str]:
    """Return the lines that tell the operator what was done."""
    label = account.get("email") or account.get("login_name") or account["id"]
    lines = [f"Password reset for {label} (user id {account['id']})."]
    if generated_password is not None:
        lines.append(f"New password: {generated_password}")
    lines.append("Every session of the account was revoked; it must sign in again.")
    status = account.get("status")
    if status != "active":
        lines.append(f"Note: the account is {status}, so it still cannot sign in.")
    return lines


async def _reset_with_database(identifier: str, new_password: str) -> dict[str, Any]:
    """Connect with the ``DB_*`` settings and run :func:`reset_password`.

    Raises:
        ResetPasswordError: For every failure the operator can act on.
    """
    import asyncpg
    from pydantic import ValidationError

    from serving.config.settings import Settings
    from serving.storage.postgres_operational import PostgresOperationalStore

    try:
        settings = Settings()
    except ValidationError as exc:
        raise ResetPasswordError(
            f"The configuration in the environment is invalid: {exc}\nNothing was changed."
        ) from exc

    where = f"{settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}"
    try:
        pool = await asyncpg.create_pool(
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            min_size=1,
            max_size=1,
            command_timeout=30,
        )
    except (OSError, asyncpg.PostgresError) as exc:
        raise ResetPasswordError(
            f"Could not connect to the database {where}: {exc}. Nothing was changed."
        ) from exc
    try:
        return await reset_password(PostgresOperationalStore(pool), identifier, new_password)
    except asyncpg.PostgresError as exc:
        raise ResetPasswordError(
            f"The database {where} reported an error: {exc}. The reset may be "
            "incomplete; run the command again."
        ) from exc
    finally:
        await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Reset the password of the account named on the command line.

    Returns:
        The process exit status: 0 on success, 1 on failure.
    """
    parser = argparse.ArgumentParser(
        prog="python -m serving.auth.reset_password",
        description=(
            "Reset an account's password and revoke its sessions. Reads only the "
            "DB_HOST, DB_PORT, DB_NAME, DB_USER and DB_PASSWORD settings."
        ),
    )
    parser.add_argument("account", help="the account's login name or email address")
    parser.add_argument(
        "--generate",
        action="store_true",
        help="generate a strong password and print it instead of prompting for one",
    )
    args = parser.parse_args(argv)
    identifier = args.account.strip()

    # Prompt before starting the event loop: a blocking prompt inside it would
    # take two Ctrl-C presses to abandon.
    try:
        new_password = generate_password() if args.generate else prompt_new_password(identifier)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing was changed.", file=sys.stderr)
        return 1
    except ResetPasswordError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        account = asyncio.run(_reset_with_database(identifier, new_password))
    except ResetPasswordError as exc:
        print(exc, file=sys.stderr)
        return 1

    for line in describe_reset(account, new_password if args.generate else None):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
