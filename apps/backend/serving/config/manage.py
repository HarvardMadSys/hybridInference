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

    python -m serving.config.manage live status
    python -m serving.config.manage live preview GROUP
    python -m serving.config.manage live activate GROUP --expect EPOCH:REVISION
        --expect-instances N
    python -m serving.config.manage live deactivate GROUP --expect EPOCH:REVISION
    python -m serving.config.manage live reset-epoch --expect EPOCH:REVISION

The ``live`` commands control the live configuration groups (see
``serving.config.live``): ``status`` shows how every registered process
stands, ``preview`` what activating a group would apply, and ``activate`` /
``deactivate`` write the group's contract on the condition that nothing changed
since the version previewed. ``reset-epoch`` follows a whole-database restore.

Run it where the backend runs, for example::

    docker exec -it hybridinference-backend python -m serving.config.manage reset JWT_ALGORITHM

Running backends apply a change within ten seconds; a setting marked
restart-required waits for the next restart.

Like the gateway at startup, it first reads ``.env`` into the environment
(without replacing a variable already set), so a value kept only there is the
one it shows and the one a reset falls back to.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any

import asyncpg
from dotenv import load_dotenv
from pydantic import ValidationError

from serving.config import app_config, app_config_registry as registry, revisions
from serving.config.settings import DatabaseSettings

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
    try:
        settings = DatabaseSettings()
    except ValidationError:
        return "(the DB_* settings are not valid)"
    return f"{settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}"


def load_stored_configuration() -> bool:
    """Load the stored configuration into this process, for a one-off command.

    For tools that run outside the gateway but read its settings, such as the
    documentation indexer: without this they would see the environment alone.
    Read-only; nothing is imported or generated. The connection gives up after
    the boot connection's deadline (``app_config_store.CONNECT_TIMEOUT_SECONDS``),
    so an unreachable host costs seconds, not minutes.

    Returns:
        Whether stored settings were loaded. A deployment without a database
        (``DB_ENABLED=false``) has none and is not contacted, and one whose
        database cannot be reached — refused, timed out, or ``DB_*`` settings
        that do not validate — keeps the environment's values, with a warning
        on stderr.
    """

    async def _load() -> None:
        async with app_config.command_line_session():
            pass

    try:
        # Inside the try: finding out imports the gateway's dependencies, which
        # build Settings, and DB_* settings that do not validate fail there.
        if not _database_enabled():
            return False
        asyncio.run(_load())
    except (*_DATABASE_ERRORS, ValidationError) as exc:
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
    if registry.is_retired(key):
        raise ManageError(
            f"{key} is retired and no longer controls anything: "
            f"{registry.RETIRED_SETTINGS[key]}. It cannot be set; `reset` removes a value an "
            "earlier version stored."
        )
    raise ManageError(f"{key} is not a known setting; `set` adds it as a custom variable.")


def _how_it_applies(entry: dict[str, Any]) -> str:
    if entry.get("apply_mode") == "managed" or (
        entry.get("group") and _group_active(entry["group"])
    ):
        return (
            f"Running backends apply it within {app_config.REFRESH_INTERVAL_SECONDS:g} seconds "
            f"through the {entry['group']} group; `live status` shows each process's "
            "acknowledgement."
        )
    if entry["restart_required"]:
        return "Restart the backend to apply it."
    return f"Running backends apply it within {app_config.REFRESH_INTERVAL_SECONDS:g} seconds."


def _group_active(name: str) -> bool:
    snapshot = app_config.last_snapshot()
    if snapshot is None:
        return False
    contract = snapshot.contracts.groups.get(name)
    return contract is not None and contract.active


def _version_text(version: dict[str, Any] | None) -> str:
    if version is None:
        return "(no version recorded)"
    return f"{version['epoch']}:{version['revision']}"


def _parse_expected(text: str) -> revisions.SourceVersion:
    try:
        return revisions.SourceVersion.parse(text)
    except ValueError:
        raise ManageError(
            f"--expect takes EPOCH:REVISION as `live status` prints it, not {text!r}."
        ) from None


def _instance_line(instance: dict[str, Any]) -> str:
    status = instance["status"] or {}
    if instance["lifecycle"] == "stopped":
        standing = "stopped"
    elif instance["stale"]:
        standing = "unknown (no heartbeat; possibly offline)"
    elif not instance["supports"]:
        standing = "unsupported (older build)"
    else:
        standing = status.get("state") or "no report"
    parts = [
        f"  {instance['instance_id']} {instance['host'] or '-'} build={instance['build']}",
        standing,
        f"reconciled={status.get('reconciled_revision')}",
        f"checked={status.get('checked_revision')}",
        f"generation={status.get('generation')}",
    ]
    if status.get("error_code"):
        parts.append(f"error={status['error_code']}")
    if status.get("cleanup_degraded"):
        parts.append("cleanup degraded")
    if status.get("pending_keys"):
        parts.append("differs: " + ",".join(status["pending_keys"]))
    return "  ".join(parts)


def status_lines(status: dict[str, Any]) -> list[str]:
    """Return the lines ``live status`` prints; no value, no secret."""
    lines = [f"app_config version {_version_text(status['version'])}"]
    for group in status["groups"]:
        contract = group["contract"]
        state = (
            "not activated"
            if contract is None
            else ("active" if contract["active"] else "deactivated")
        )
        coverage = group["coverage"]
        lines.append(
            f"{group['name']} v{group['schema_version']}: {state}; "
            f"{coverage['converged']} of {coverage['registered']} registered process(es) "
            f"reconciled to revision {status['version']['revision'] if status['version'] else '-'}"
            f" ({coverage['unknown']} unknown, {coverage['unsupported']} unsupported, "
            f"{coverage['failed']} failed, {coverage['applying']} applying, "
            f"{coverage['dormant']} dormant). Coverage unverified: an older build does not "
            "register; compare with the deployment's process inventory."
        )
        lines.extend(_instance_line(instance) for instance in group["instances"])
    for name in status["unknown_groups"]:
        lines.append(f"{name}: in the contract, unknown to this build")
    return lines


def preview_lines(preview: dict[str, Any]) -> list[str]:
    """Return the lines ``live preview`` prints; a secret only as set or not set."""
    lines = [
        f"{preview['group']} at version {_version_text(preview['version'])}"
        f" ({'active' if preview['active'] else 'not active'})",
        "Saved targets activation applies:",
    ]
    for key in preview["keys"]:
        if key["secret"]:
            value = "(secret, set)" if key["is_set"] else "(secret, not set)"
        else:
            value = json.dumps(key["saved"])
        lines.append(f"  {key['key']}={value}  [{key['source']}]")
    lines.append("Processes whose values in use differ:")
    differing = [item for item in preview["instances"] if item["pending_keys"]]
    for item in differing:
        lines.append(f"  {item['instance_id']}: {', '.join(item['pending_keys'])}")
    if not differing:
        lines.append("  none reported")
    lines.append(
        "Activating applies these saved values at once and may change where alerts go. "
        f"Activate with: live activate {preview['group']} --expect "
        f"{_version_text(preview['version'])}"
    )
    return lines


async def run_live(args: argparse.Namespace) -> list[str]:
    """Run one ``live`` command; return what to print.

    Raises:
        ManageError: The command cannot run as asked.
    """
    from serving.config import live_control

    async with app_config.command_line_session():
        store = app_config.attached_store()
        assert store is not None
        try:
            if args.live_command == "status":
                return status_lines(await live_control.fetch_status(store))
            if args.live_command == "preview":
                status, entries = await live_control.consistent_read(
                    store,
                    refresh=app_config.refresh,
                    entries=lambda: app_config.describe()["entries"],
                    current_version=app_config.source_version,
                )
                return preview_lines(live_control.preview(status, entries, args.group))
            expected = _parse_expected(args.expect)
            if args.live_command == "activate":
                version = await live_control.activate(
                    store,
                    args.group,
                    expected=expected,
                    changed_by=UPDATED_BY,
                    expect_instances=args.expect_instances,
                )
                return [
                    f"{args.group} activated at {version.epoch}:{version.revision}. Running "
                    f"processes apply it within {app_config.REFRESH_INTERVAL_SECONDS:g} seconds; "
                    "check `live status`."
                ]
            if args.live_command == "deactivate":
                version = await live_control.deactivate(
                    store, args.group, expected=expected, changed_by=UPDATED_BY
                )
                return [
                    f"{args.group} deactivated at {version.epoch}:{version.revision}. Processes "
                    "that applied it keep what they applied (dormant-frozen) until they restart."
                ]
            version = await live_control.reset_epoch(store, expected=expected)
            return [
                f"New epoch {version.epoch} (revision {version.revision}). Every registration "
                "was removed; running processes register again within "
                f"{app_config.REFRESH_INTERVAL_SECONDS:g} seconds."
            ]
        except live_control.ControlError as exc:
            raise ManageError(str(exc)) from None
        except revisions.VersionConflict as exc:
            raise ManageError(
                f"The configuration changed since {exc.expected.epoch}:{exc.expected.revision}"
                f" (now {_version_text(exc.current.as_dict() if exc.current else None)}). "
                "Preview again."
            ) from None


async def run(args: argparse.Namespace) -> list[str]:
    """Run one parsed command against the stored configuration; return what to print.

    Raises:
        ManageError: The command cannot run as asked.
        app_config.ConfigUpdateError: The change was refused.
    """
    if args.command == "live":
        return await run_live(args)
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
        help=(
            "mark the variable secret (discovered, numbered and custom variables; "
            "only ever turns secrecy on)"
        ),
    )
    reset = commands.add_parser(
        "reset", help="remove the stored value; the environment or the default applies"
    )
    reset.add_argument("key", help="the setting's name")
    live = commands.add_parser("live", help="live configuration groups: status and activation")
    live_commands = live.add_subparsers(dest="live_command", required=True, metavar="ACTION")
    live_commands.add_parser("status", help="show how every registered process stands")
    preview = live_commands.add_parser("preview", help="show what activating a group applies")
    preview.add_argument("group", help="the group, e.g. alerts.runtime")
    activate = live_commands.add_parser("activate", help="activate a group's contract")
    activate.add_argument("group", help="the group, e.g. alerts.runtime")
    activate.add_argument("--expect", required=True, help="the EPOCH:REVISION previewed")
    activate.add_argument(
        "--expect-instances",
        type=int,
        required=True,
        help=(
            "the number of serving processes the deployment's own inventory counts; "
            "a process on an older build does not register, so this cannot be inferred"
        ),
    )
    deactivate = live_commands.add_parser("deactivate", help="deactivate a group's contract")
    deactivate.add_argument("group", help="the group")
    deactivate.add_argument("--expect", required=True, help="the EPOCH:REVISION seen")
    reset_epoch = live_commands.add_parser(
        "reset-epoch", help="start a new epoch after restoring the whole database"
    )
    reset_epoch.add_argument("--expect", required=True, help="the EPOCH:REVISION seen")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command named on the command line.

    Returns:
        The process exit status: 0 on success, 1 when the command was refused
        or the database could not be reached.
    """
    args = build_parser().parse_args(argv)
    # Before anything is resolved, read .env the way the gateway does at startup
    # (bootstrap.initialize): a value only .env holds, as under a systemd unit
    # without EnvironmentFile, is then the environment value this tool shows,
    # and the one a reset falls back to. A variable already set is kept.
    load_dotenv()
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
