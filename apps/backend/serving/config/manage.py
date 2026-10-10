"""Read and change the database-backed configuration from a shell.

The way back in when a stored value keeps administrators out of the console:
it connects with the ``DB_*`` environment alone and applies the console's own
rules — the same validation, the same refusal to change an immutable secret.
A secret's value is never printed, only whether it is set.

Usage::

    python -m serving.config.manage list
    python -m serving.config.manage get KEY
    python -m serving.config.manage set KEY VALUE [--secret]
    python -m serving.config.manage reset KEY

Run it where the backend runs, for example::

    docker exec -it hybridinference-backend python -m serving.config.manage reset JWT_ALGORITHM

Running backends apply a change within ten seconds; a setting marked
restart-required waits for the next restart.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any

import asyncpg

from serving.config import app_config, app_config_registry as registry
from serving.config.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The ``updated_by`` this tool records.
UPDATED_BY = "cli"

# Failures that mean "the database could not be reached or refused us".
_DATABASE_ERRORS = (OSError, asyncio.TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError)


class ManageError(Exception):
    """The command did not run; the message says why."""


def _database_enabled() -> bool:
    from serving.servers.deps import database_enabled

    return database_enabled()


def _where() -> str:
    settings = get_settings()
    return f"{settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}"


def load_stored_configuration() -> bool:
    """Load the stored configuration into this process, for a one-off command.

    For tools that run outside the gateway but read its settings, such as the
    documentation indexer: without this they would see the environment alone.
    Read-only; nothing is imported or generated.

    Returns:
        Whether stored settings were loaded. A deployment without a database
        has none, and one whose database cannot be reached keeps the
        environment's values, with a warning on stderr.
    """
    if not _database_enabled():
        return False

    async def _load() -> None:
        async with app_config.command_line_session():
            pass

    try:
        asyncio.run(_load())
    except _DATABASE_ERRORS as exc:
        print(
            f"warning: stored settings not loaded, using the environment's "
            f"(database {_where()}: {exc})",
            file=sys.stderr,
        )
        return False
    return True


def _render(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _notes(entry: dict[str, Any]) -> list[str]:
    notes = []
    if entry["invalid"]:
        notes.append(f"invalid, not applied: {entry['invalid']}")
    if entry["missing"]:
        notes.append("required but empty")
    if entry["pending_restart"]:
        notes.append("changed; applies after a restart")
    elif entry["restart_required"]:
        notes.append("applies after a restart")
    if entry["environment_ignored"]:
        notes.append("overrides a different environment value")
    if entry["immutable"]:
        notes.append("cannot be changed once set")
    return notes


def describe_entry(entry: dict[str, Any]) -> list[str]:
    """Return the lines ``get`` prints for one entry."""
    if entry["secret"]:
        value = "(secret, set)" if entry["is_set"] else "(secret, not set)"
    else:
        value = _render(entry["value"])
    lines = [f"{entry['key']}={value}", f"source: {entry['source']}"]
    if entry["updated_at"] is not None:
        lines.append(f"updated: {entry['updated_at']} by {entry['updated_by']}")
    notes = _notes(entry)
    if notes:
        lines.append("notes: " + "; ".join(notes))
    return lines


def list_entries(body: dict[str, Any]) -> list[str]:
    """Return the lines ``list`` prints: one per entry, secrets as set or not set."""
    lines = []
    for entry in body["entries"]:
        if entry["secret"]:
            value = "(set)" if entry["is_set"] else "(not set)"
        elif isinstance(entry["value"], str):
            # Quoted, so an empty value and a multi-line one stay readable.
            value = json.dumps(entry["value"])
        else:
            value = _render(entry["value"])
        line = f"{entry['key']}={value}  [{entry['source']}]"
        notes = _notes(entry)
        if notes:
            line += "  (" + "; ".join(notes) + ")"
        lines.append(line)
    return lines


def _find(key: str) -> dict[str, Any]:
    for entry in app_config.describe()["entries"]:
        if entry["key"] == key:
            return entry
    if registry.is_environment_only(key):
        raise ManageError(
            f"{key} stays in the environment ({registry.ENVIRONMENT_ONLY[key]}); set it there."
        )
    if registry.is_runtime_setting_name(key):
        raise ManageError(f"{key} is a runtime setting; change it on the console's Settings tab.")
    raise ManageError(f"{key} is not a known setting; `set` adds it as a custom variable.")


def _how_it_applies(entry: dict[str, Any]) -> str:
    if entry["restart_required"]:
        return "Restart the backend to apply it."
    return f"Running backends apply it within {app_config.REFRESH_INTERVAL_SECONDS:g} seconds."


async def run(args: argparse.Namespace) -> list[str]:
    """Run one parsed command against the stored configuration; return what to print.

    Raises:
        ManageError: The command cannot run as asked.
        app_config.ConfigUpdateError: The change was refused.
    """
    async with app_config.command_line_session():
        if args.command == "list":
            return list_entries(app_config.describe())
        if args.command == "get":
            return describe_entry(_find(args.key))
        if args.command == "set":
            value = app_config.parse_text_value(args.key, args.value)
            await app_config.update(
                {args.key: value}, {args.key: args.secret}, updated_by=UPDATED_BY
            )
            entry = _find(args.key)
            return [*describe_entry(entry), _how_it_applies(entry)]
        await app_config.delete(args.key, updated_by=UPDATED_BY)
        try:
            entry = _find(args.key)
        except ManageError:
            return [f"{args.key} removed."]
        return [*describe_entry(entry), _how_it_applies(entry)]


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m serving.config.manage",
        description=(
            "Read and change the gateway's stored configuration. Reads only the DB_HOST, "
            "DB_PORT, DB_NAME, DB_USER and DB_PASSWORD settings, and never prints a "
            "secret's value."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("list", help="list every setting")
    get = commands.add_parser("get", help="show one setting")
    get.add_argument("key", help="the setting's name, e.g. JWT_ALGORITHM")
    store = commands.add_parser("set", help="store a value, validated as the console does")
    store.add_argument("key", help="the setting's name")
    store.add_argument("value", help="the value: true/false, a number, or text")
    store.add_argument(
        "--secret",
        action="store_true",
        help="mark a custom variable this command adds as secret",
    )
    reset = commands.add_parser(
        "reset", help="remove the stored value; the environment or the default applies"
    )
    reset.add_argument("key", help="the setting's name")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command named on the command line.

    Returns:
        The process exit status: 0 on success, 1 when the command was refused
        or the database could not be reached.
    """
    args = build_parser().parse_args(argv)
    if not _database_enabled():
        print(
            "This deployment runs without a database (DB_ENABLED=false): its settings "
            "are read from the environment only.",
            file=sys.stderr,
        )
        return 1
    try:
        lines = asyncio.run(run(args))
    except app_config.ConfigUpdateError as exc:
        print(f"Refused: {exc.detail}", file=sys.stderr)
        return 1
    except ManageError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except _DATABASE_ERRORS as exc:
        print(f"Could not use the database {_where()}: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
