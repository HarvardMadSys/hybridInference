"""Status, activation and epoch control for live configuration groups.

What the console shows and the deployment commands do
(``python -m serving.config.manage live ...``):

- :func:`fetch_status` — the saved source version, each group's persisted
  contract and how every registered instance stands on it, read in one
  database snapshot. Coverage counts registered processes only, so it is never
  reported as verified: an older build does not register.
- :func:`preview` — before activation, what activating would change: each key
  of the group with its saved target (a secret only as set or not set), and
  the instances whose state in use differs from it, by key name.
- :func:`activate` / :func:`deactivate` — write the contract and advance the
  revision in one transaction, conditional on the version the operator
  previewed. Activation refuses while a live registered process lacks the
  group's schema version.
- :func:`reset_epoch` — after a whole-database restore.

None of these reads, returns or logs a configuration value of a secret.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.config import app_config_registry as registry, instances, revisions

if TYPE_CHECKING:
    from serving.config.app_config_store import AppConfigStore


class ControlError(Exception):
    """A control command was refused; the message says why and what to do."""


async def _read(
    conn: Any,
) -> tuple[revisions.SourceVersion | None, revisions.Contracts, list[dict[str, Any]]]:
    version, contracts = await revisions.read_version(conn)
    found = await instances.fetch_instances(conn)
    return version, contracts, found


async def fetch_status(
    store: AppConfigStore, *, this_instance: str | None = None
) -> dict[str, Any]:
    """Return the source version, every group's contract, and each instance's report.

    One read-only REPEATABLE READ transaction, so the counts and the version
    describe the same moment.
    """
    async with (
        store.connection() as conn,
        conn.transaction(isolation="repeatable_read", readonly=True),
    ):
        version, contracts, found = await _read(conn)
    groups = []
    for name, manifest in registry.MANAGED_GROUPS.items():
        contract = contracts.groups.get(name)
        schema_version = (
            contract.schema_version if contract is not None else manifest.schema_version
        )
        rows = []
        for instance in found:
            report = (instance["groups"] or {}).get(name)
            rows.append(
                {
                    "instance_id": instance["instance_id"],
                    "this_process": instance["instance_id"] == this_instance,
                    "build": instance["build"],
                    "host": (instance["capabilities"].get("details") or {}).get("host"),
                    "lifecycle": instance["lifecycle"],
                    "started_at": instance["started_at"],
                    "heartbeat_age_seconds": instance["heartbeat_age_seconds"],
                    "stale": instance["stale"],
                    "supports": instances.supports(instance["capabilities"], name, schema_version),
                    "status": report if isinstance(report, dict) else None,
                }
            )
        groups.append(
            {
                "name": name,
                "keys": list(manifest.keys),
                "schema_version": schema_version,
                "contract": (
                    {
                        "active": contract.active,
                        "schema_version": contract.schema_version,
                        "changed_at": contract.changed_at,
                        "changed_by": contract.changed_by,
                    }
                    if contract is not None
                    else None
                ),
                "coverage": instances.coverage(
                    found,
                    group=name,
                    schema_version=schema_version,
                    epoch=version.epoch if version is not None else None,
                    revision=version.revision if version is not None else None,
                ),
                "instances": rows,
            }
        )
    unknown = sorted(name for name in contracts.groups if name not in registry.MANAGED_GROUPS)
    return {
        "source": revisions.APP_CONFIG_SOURCE,
        "version": version.as_dict() if version is not None else None,
        "this_instance": this_instance,
        "stale_after_seconds": instances.STALE_AFTER_SECONDS,
        "groups": groups,
        "unknown_groups": unknown,
    }


def _manifest(name: str) -> revisions.GroupContract:
    manifest = registry.MANAGED_GROUPS.get(name)
    if manifest is None:
        known = ", ".join(sorted(registry.MANAGED_GROUPS))
        raise ControlError(f"{name} is not a live configuration group this build knows ({known}).")
    return manifest


async def consistent_read(
    store: AppConfigStore,
    *,
    refresh: Any,
    entries: Any,
    current_version: Any,
    attempts: int = 3,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the status and the described entries, both at one version.

    A preview's saved targets come from this process's configuration and its
    counts from the database, read separately; a write between the two would
    let an operator activate a version they never saw. Both are re-read until
    they name the same version.

    Args:
        store: The configuration store.
        refresh: Re-reads the process's configuration (``app_config.refresh``).
        entries: Returns the process's described entries.
        current_version: Returns the version those entries describe.
        attempts: Reads before giving up.

    Raises:
        ControlError: The configuration kept changing.
    """
    for _ in range(attempts):
        await refresh()
        before = current_version()
        status = await fetch_status(store)
        described = entries()
        if (
            before is not None
            and status["version"] == before.as_dict()
            and current_version() == before
        ):
            return status, described
    raise ControlError("The configuration kept changing while it was read; preview again.")


def preview(status: dict[str, Any], entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """Describe what activating *name* would apply, from :func:`fetch_status` and ``describe()``.

    A secret shows only whether it is set; per instance, only the names of the
    keys whose state in use differs from the saved target.
    """
    manifest = _manifest(name)
    by_key = {entry["key"]: entry for entry in entries}
    keys = []
    for key in manifest.keys:
        entry = by_key.get(key)
        if entry is None:
            continue
        keys.append(
            {
                "key": key,
                "secret": bool(entry["secret"]),
                "saved": None if entry["secret"] else entry["value"],
                "is_set": bool(entry["is_set"]),
                "source": entry["source"],
            }
        )
    group = next(group for group in status["groups"] if group["name"] == name)
    changes = []
    for instance in group["instances"]:
        report = instance["status"] or {}
        if instance["lifecycle"] == "stopped":
            continue
        changes.append(
            {
                "instance_id": instance["instance_id"],
                "stale": instance["stale"],
                "supports": instance["supports"],
                "state": report.get("state"),
                "pending_keys": list(report.get("pending_keys") or []),
            }
        )
    return {
        "group": name,
        "version": status["version"],
        "active": bool(group["contract"] and group["contract"]["active"]),
        "keys": keys,
        "instances": changes,
        "coverage": group["coverage"],
    }


def _live_instances(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        instance
        for instance in found
        if instance["lifecycle"] != "stopped" and not instance["stale"]
    ]


async def activate(
    store: AppConfigStore,
    name: str,
    *,
    expected: revisions.SourceVersion,
    changed_by: str,
    expect_instances: int,
) -> revisions.SourceVersion:
    """Activate group *name*: its saved values start applying without a restart.

    Registration alone cannot prove coverage — a process on an older build
    never registers — so the operator states how many serving processes the
    deployment's own inventory counts, and activation proceeds only when
    exactly that many live registered processes support the group and none
    that is live lacks it. Absence is never taken as safety.

    Args:
        store: The configuration store.
        name: The group.
        expected: The version the operator previewed; anything newer refuses.
        changed_by: Recorded on the contract.
        expect_instances: The number of serving processes the deployment's
            inventory counts; at least one.

    Raises:
        ControlError: Refused, with the reason.
        revisions.VersionConflict: *expected* is no longer current.
    """
    manifest = _manifest(name)
    if expect_instances < 1:
        raise ControlError(
            "--expect-instances must be the number of serving processes the deployment runs, "
            "at least 1."
        )
    async with (
        store.transaction(expected=expected) as transaction,
        transaction.connection() as conn,
    ):
        found = await instances.fetch_instances(conn)
        live = _live_instances(found)
        lacking = [
            instance["instance_id"]
            for instance in live
            if not instances.supports(instance["capabilities"], name, manifest.schema_version)
        ]
        if lacking:
            raise ControlError(
                f"{len(lacking)} live registered process(es) do not support {name} "
                f"v{manifest.schema_version} ({', '.join(lacking)}). Deploy the bridge build "
                "everywhere first."
            )
        capable = len(live) - len(lacking)
        if capable != expect_instances:
            raise ControlError(
                f"The deployment inventory counts {expect_instances} process(es), but "
                f"{capable} live registered process(es) support {name}. A process on an "
                "older build does not register; check the inventory before activating."
            )
        version, _ = await revisions.set_group_active(
            conn, manifest, active=True, expected=expected, changed_by=changed_by
        )
    return version


async def deactivate(
    store: AppConfigStore,
    name: str,
    *,
    expected: revisions.SourceVersion,
    changed_by: str,
) -> revisions.SourceVersion:
    """Deactivate group *name*.

    A running process that applied the group keeps the state it published
    (``dormant-frozen``) until it restarts; a process that starts afterwards
    boots on the saved values, as before activation. The two can differ until
    every process has restarted.

    Raises:
        revisions.VersionConflict: *expected* is no longer current.
    """
    manifest = _manifest(name)
    async with (
        store.transaction(expected=expected) as transaction,
        transaction.connection() as conn,
    ):
        version, _ = await revisions.set_group_active(
            conn, manifest, active=False, expected=expected, changed_by=changed_by
        )
    return version


async def reset_epoch(
    store: AppConfigStore, *, expected: revisions.SourceVersion
) -> revisions.SourceVersion:
    """Start a new epoch after a whole-database restore; registrations are rebuilt.

    Raises:
        revisions.VersionConflict: *expected* is no longer current.
    """
    async with (
        store.transaction(expected=expected) as transaction,
        transaction.connection() as conn,
    ):
        return await revisions.reset_epoch(conn, expected=expected)
