"""Database-backed application configuration.

Settings are resolved **database row → environment → built-in default**, and a
row wins even when its value is empty. The registry of settings lives in
:mod:`serving.config.app_config_registry`, the table in
:mod:`serving.config.app_config_store`.

How values reach the code that reads them:

- :func:`config_value` is the one synchronous resolver, for everything read
  outside ``Settings``.
- Entries backed by a ``Settings`` field are overlaid **in place** on the
  ``get_settings()`` object, which several modules hold a reference to: a
  candidate ``Settings`` is built with the database values as init kwargs, so
  every validator and derived field recomputes, and its values are copied onto
  the live object. A value that fails validation is skipped and reported as
  invalid rather than stopping the gateway.
- Modules that keep a setting in a module-level constant register with
  :func:`on_change` and recompute it after every change.

A setting marked ``restart_required`` is captured at startup, so the process
keeps serving the value it booted with — from :func:`config_value` and the
overlay alike — and :func:`get_config_health` lists it as pending a restart
until then. Every other setting applies as soon as it is stored: at once in the
process that served the write, and within :data:`REFRESH_INTERVAL_SECONDS` in
every other one.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from serving.config import app_config_registry as registry
from serving.config.app_config_store import AppConfigStore, ConfigRow, connect
from serving.config.settings import (
    CONNECTION_FIELDS,
    DatabaseSettings,
    Settings,
    get_settings,
    invalid_environment,
    invalid_fields,
    rules_deferred,
)
from serving.utils import logging as logging_utils

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence

    from serving.config.app_config_registry import ConfigEntry, DiscoveredReference

logger = logging_utils.get_logger(__name__)

#: How often every process re-reads ``app_config``, so workers converge after an
#: administrator's write.
REFRESH_INTERVAL_SECONDS = 10.0

#: Length of a generated secret, in bytes of randomness.
_GENERATED_SECRET_BYTES = 48


@dataclass(frozen=True)
class ConfigHealth:
    """Configuration problems an administrator needs to act on."""

    missing: tuple[str, ...] = ()
    pending_restart: tuple[str, ...] = ()

    @property
    def incomplete(self) -> bool:
        """Return whether a required setting has no value."""
        return bool(self.missing)


class ConfigBootstrapError(RuntimeError):
    """The stored configuration cannot be made safe to start on."""


class ConfigUpdateError(Exception):
    """An administrator's change was refused.

    Attributes:
        status_code: The HTTP status the admin API answers with.
        detail: Operator-facing reason; never contains a secret's value.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ConfigChange:
    """One stored change, described for the admin audit log."""

    key: str
    secret: bool
    old_value: Any = None
    new_value: Any = None

    def audit_details(self) -> dict[str, Any]:
        """Return the audit record; a secret's values are never part of it."""
        if self.secret:
            return {"key": self.key, "changed": True}
        return {"key": self.key, "old_value": self.old_value, "new_value": self.new_value}


@dataclass
class _State:
    """Everything loaded, replaced wholesale by :func:`reset_state`."""

    #: ``app_config`` as last read.
    rows: dict[str, ConfigRow] = field(default_factory=dict)
    #: The rows this process booted with; restart-required settings keep them.
    boot_rows: dict[str, ConfigRow] = field(default_factory=dict)
    #: The rows :func:`config_value` serves: current for live settings, boot
    #: rows for the rest.
    effective: dict[str, ConfigRow] = field(default_factory=dict)
    #: Settings whose changes apply without a restart.
    live_keys: frozenset[str] = frozenset()
    entries: dict[str, ConfigEntry] = field(default_factory=dict)
    discovered: dict[str, DiscoveredReference] = field(default_factory=dict)
    #: Stored values that could not be applied, with the reason.
    invalid: dict[str, str] = field(default_factory=dict)
    health: ConfigHealth = field(default_factory=ConfigHealth)
    database_enabled: bool = False
    #: Rows have been read from the database at least once.
    loaded: bool = False
    #: A database value has been overlaid on ``Settings`` at least once.
    overlaid: bool = False
    #: Stored ``Settings``-backed values the boot-time overlay put in force.
    applied_at_boot: frozenset[str] = frozenset()
    #: Bumped by every apply, so a slower read never replaces a newer one.
    generation: int = 0
    store: AppConfigStore | None = None
    refresh_task: asyncio.Task[None] | None = None


_STATE = _State()
_LISTENERS: list[Callable[[], None]] = []


def config_value(key: str, default: str | None = None) -> str | None:
    """Resolve a setting: database row, then environment, then *default*.

    Cheap enough for a request path: one dictionary lookup, then the
    environment. Before the configuration loads, and in a database-free
    deployment, it is exactly ``os.environ.get(key, default)``.
    """
    row = _STATE.effective.get(key)
    if row is not None:
        return row.value
    return os.environ.get(key, default)


# LOG_LEVEL and LOG_FORMAT are database-backed too. Logging cannot import this
# module (everything imports logging), so hand it the resolver instead.
logging_utils.use_setting_resolver(config_value)


def get_config_health() -> ConfigHealth:
    """Return the cached configuration health (synchronous)."""
    return _STATE.health


def loaded_from_database() -> bool:
    """Return whether stored settings have been read from the database."""
    return _STATE.loaded


def on_change(listener: Callable[[], None]) -> Callable[[], None]:
    """Call *listener* after every configuration change, and return it.

    For modules that keep a setting in a module-level constant: the listener
    re-reads it through :func:`config_value`. A listener that raises is logged
    and skipped; the others still run.
    """
    _LISTENERS.append(listener)
    return listener


def _notify() -> None:
    for listener in list(_LISTENERS):
        try:
            listener()
        except Exception:
            logger.warning("Configuration listener %r failed", listener, exc_info=True)


# --- values ------------------------------------------------------------------

_TRUE_WORDS = frozenset({"1", "on", "t", "true", "y", "yes"})
_FALSE_WORDS = frozenset({"0", "off", "f", "false", "n", "no"})


def _parse_bool(raw: str) -> bool:
    """Parse a boolean the way ``Settings`` does."""
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError(raw)


def _typed(entry: ConfigEntry, raw: str | None) -> Any:
    """Return *raw* in the entry's typed form, or *raw* itself when it does not parse."""
    if raw is None:
        return None
    if entry.type in ("str", "text", "list"):
        return raw
    if not raw.strip():
        return None
    try:
        if entry.type == "bool":
            return raw != "0" if entry.flag else _parse_bool(raw)
        if entry.type == "int":
            return int(raw.strip())
        return float(raw.strip())
    except ValueError:
        return raw


def _comparable(entry: ConfigEntry, raw: str | None) -> Any:
    """Normalize a value so two spellings of the same setting compare equal."""
    if raw is None:
        return None
    if entry.type == "list":
        return tuple(item.strip() for item in raw.split(",") if item.strip())
    return _typed(entry, raw)


def _value_problem(entry: ConfigEntry, raw: str | None) -> str | None:
    """Return why *raw* is not a valid value for *entry*, if it is not.

    Beyond the type, this enforces the registry's own limits — the choices,
    bounds and non-blank secrets that keep one stored value from breaking sign-in
    or every request. ``Settings`` validation is checked separately.
    """
    if raw is None:
        return None
    if not raw.strip():
        if entry.generated:
            return "must not be blank"
        if entry.choices:
            return "must be one of " + ", ".join(entry.choices)
        return None
    typed = _typed(entry, raw)
    if entry.type in ("bool", "int", "float") and isinstance(typed, str):
        return {
            "bool": "must be true or false",
            "int": "must be an integer",
            "float": "must be a number",
        }[entry.type]
    if entry.type in ("int", "float"):
        if not math.isfinite(typed):
            return "must be a finite number"
        if entry.minimum is not None and typed < entry.minimum:
            return f"must be at least {entry.minimum:g}"
        if entry.maximum is not None and typed > entry.maximum:
            return f"must be at most {entry.maximum:g}"
    if entry.choices:
        # Compared as stored, without trimming: the value is used as stored.
        if entry.case_sensitive:
            allowed = raw in entry.choices
        else:
            allowed = raw.lower() in {choice.lower() for choice in entry.choices}
        if not allowed:
            return "must be one of " + ", ".join(entry.choices)
    return None


def _environ_value(key: str, entry: ConfigEntry | None) -> str | None:
    """Return *key*'s environment value as the code that reads it would find it.

    ``Settings`` matches variable names case-insensitively, so a lower-case
    ``api_key_secret=`` line has always configured ``API_KEY_SECRET``; the exact
    upper-case name wins when both are present. Every other setting is read
    with the exact name.
    """
    value = os.environ.get(key)
    if value is not None or entry is None or entry.field is None:
        return value
    folded = key.lower()
    for name, candidate in os.environ.items():
        if name.lower() == folded:
            return candidate
    return None


def _resolve(rows: Mapping[str, ConfigRow], key: str, entry: ConfigEntry | None) -> str | None:
    """Resolve *key* against *rows*, then the environment, then the default."""
    row = rows.get(key)
    if row is not None:
        return row.value
    value = _environ_value(key, entry)
    if value is not None:
        return value
    return entry.default if entry is not None else None


def _configured(key: str) -> str:
    """Return the value currently configured for *key*, or ""."""
    return _resolve(_STATE.rows, key, _STATE.entries.get(key)) or ""


# --- Settings overlay --------------------------------------------------------


def _init_kwargs(values: Mapping[str, str]) -> dict[str, str]:
    kwargs: dict[str, str] = {}
    for key, raw in values.items():
        entry = registry.static_entry(key)
        if entry is not None and entry.field is not None:
            kwargs[registry.settings_init_name(entry.field)] = raw
    return kwargs


def _error_message(error: Mapping[str, Any]) -> str:
    message = str(error.get("msg") or "invalid value")
    return message.removeprefix("Value error, ")


def _blame(exc: ValidationError, candidates: Iterable[str]) -> dict[str, str]:
    """Map field-level validation errors back to the keys that caused them."""
    owners: dict[str, str] = {}
    for key in candidates:
        entry = registry.static_entry(key)
        if entry is not None and entry.field is not None:
            owners[entry.field] = key
            owners[registry.settings_init_name(entry.field)] = key
    blamed: dict[str, str] = {}
    for error in exc.errors():
        location = error.get("loc") or ()
        key = owners.get(str(location[0])) if location else None
        if key is not None and key not in blamed:
            blamed[key] = _error_message(error)
    return blamed


@dataclass(frozen=True)
class _InForce:
    """What a set of stored values puts in force over the environment.

    Built by :func:`_in_force`, which is how the overlay applies stored values,
    so the overlay, the boot-time import and every write judge a configuration
    the same way.
    """

    #: The settings in force; ``None`` when the environment does not validate
    #: whatever is stored (an error no stored value is to blame for).
    settings: Settings | None
    #: Stored values that cannot apply, with the reason.
    refused: dict[str, str]
    #: Environment values not valid on their own that no applied stored value
    #: replaces, with the reason. Their default applies instead.
    set_aside: dict[str, str]
    #: The rules between settings the configuration in force still breaks, with
    #: the settings each one ties. Only the environment breaks one here: a
    #: stored value that would is refused instead.
    broken: dict[str, tuple[str, ...]]
    #: The settings each rule named in ``refused`` or ``broken`` ties.
    rules: dict[str, tuple[str, ...]]


def _rule_keys(fields: Iterable[str]) -> tuple[str, ...]:
    return tuple(registry.settings_env_name(name) for name in fields)


def _in_force(stored: Mapping[str, str]) -> _InForce:
    """Judge stored ``Settings``-backed values over the environment, as the overlay applies them.

    - A stored value that is not valid on its own is refused.
    - An environment value that is not valid on its own is set aside, and its
      default applies, unless an applied stored value replaces it.
    - Then the rules between settings. The stored values are taken in the order
      given, and one is refused when it breaks a rule that holds without it. A
      refused value is tried again after each acceptance, so it is not refused
      merely because the value it needs comes later.
    - A rule the result still breaks is one the environment breaks and no stored
      value repairs. Startup refuses it (:func:`require_valid_environment`).

    Args:
        stored: ``Settings``-backed keys and their stored values, in registry
            order (:func:`_stored_settings`).
    """
    refused: dict[str, str] = {}
    rules: dict[str, tuple[str, ...]] = {}
    # Setting -> why its environment value is not valid, found as builds fail.
    unusable: dict[str, str] = {}

    def build(values: Mapping[str, str]) -> Settings:
        """Build *values* over the environment, its invalid values set aside.

        Raises:
            ValidationError: A value in *values* is not valid, or the
                environment fails in a way that cannot be set aside.
        """
        while True:
            defaults = {
                registry.settings_init_name(name): Settings.model_fields[name].get_default(
                    call_default_factory=True
                )
                for name in unusable
                if registry.settings_env_name(name) not in values
            }
            try:
                with rules_deferred():
                    return Settings(**{**defaults, **_init_kwargs(values)})
            except ValidationError as exc:
                found = {
                    name: problem
                    for name, problem in invalid_fields(exc).items()
                    if registry.settings_env_name(name) not in values
                    and name not in unusable
                    and name not in CONNECTION_FIELDS
                    and not Settings.model_fields[name].is_required()
                }
                if not found:
                    raise
                unusable.update(found)

    def broken_by(settings: Settings) -> dict[str, tuple[str, ...]]:
        broken = {rule: _rule_keys(fields) for rule, fields in settings.broken_rules().items()}
        rules.update(broken)
        return broken

    def result(settings: Settings | None, accepted: Mapping[str, str]) -> _InForce:
        set_aside = {
            key: problem
            for name, problem in unusable.items()
            if (key := registry.settings_env_name(name)) not in accepted
        }
        broken = broken_by(settings) if settings is not None else {}
        return _InForce(settings, refused, set_aside, broken, rules)

    # Field-level errors name their value: refuse it and try again.
    values = dict(stored)
    while True:
        try:
            candidate = build(values)
            break
        except ValidationError as exc:
            blamed = _blame(exc, values)
            if not blamed:
                return result(None, values)
            for key, message in blamed.items():
                refused[key] = message
                del values[key]
    if not broken_by(candidate):
        return result(candidate, values)
    # A rule between settings is broken, and it names no single value: take the
    # stored values one at a time.
    try:
        accepted: dict[str, str] = {}
        candidate = build(accepted)
    except ValidationError:
        return result(None, values)
    broken = broken_by(candidate)
    waiting = dict(values)
    reasons: dict[str, str] = {}
    progress = True
    while progress and waiting:
        progress = False
        for key, value in list(waiting.items()):
            trial = {**accepted, key: value}
            try:
                built = build(trial)
            except ValidationError as exc:
                errors = exc.errors()
                reasons[key] = _error_message(errors[0]) if errors else "invalid value"
                continue
            trial_broken = broken_by(built)
            added = [rule for rule in trial_broken if rule not in broken]
            if added:
                reasons[key] = added[0]
                continue
            accepted, candidate, broken = trial, built, trial_broken
            del waiting[key]
            progress = True
    for key in waiting:
        refused[key] = reasons[key]
    return result(candidate, accepted)


def _stored_settings(
    rows: Mapping[str, ConfigRow], entries: Mapping[str, ConfigEntry]
) -> dict[str, str]:
    """Return the stored ``Settings``-backed values the registry accepts, in its order."""
    return {
        key: rows[key].value
        for key, entry in entries.items()
        if entry.field is not None
        and key in rows
        and _value_problem(entry, rows[key].value) is None
    }


def _copy_settings(candidate: Settings, field_names: Iterable[str]) -> None:
    """Copy *field_names* from *candidate* onto the live settings object."""
    live = get_settings()
    fields_set = set(live.model_fields_set)
    for name in field_names:
        object.__setattr__(live, name, getattr(candidate, name))
        if name in candidate.model_fields_set:
            fields_set.add(name)
        else:
            fields_set.discard(name)
    object.__setattr__(live, "__pydantic_fields_set__", fields_set)


def _overlay_settings(*, boot: bool, refused: Mapping[str, str]) -> dict[str, str]:
    """Apply stored values of ``Settings``-backed entries; return those that failed.

    Args:
        boot: Whether this is the load the process starts on.
        refused: Stored values the registry already refused, left out here.
    """
    state = _STATE
    backed = [entry for entry in state.entries.values() if entry.field is not None]
    stored = {
        entry.key: state.rows[entry.key].value
        for entry in backed
        if entry.key in state.rows and entry.key not in refused
    }
    if not stored and not state.overlaid:
        # Nothing stored has ever been applied: the live object already holds
        # the environment's values, and rebuilding it would only discard any
        # adjustment made to it in-process.
        return {}
    in_force = _in_force(stored)
    candidate, invalid = in_force.settings, dict(in_force.refused)
    if candidate is None:
        logger.error("Configuration overlay skipped: the environment itself does not validate")
        return invalid
    if not boot:
        frozen = [entry.key for entry in backed if entry.key not in state.live_keys]
        if any(state.rows.get(key) != state.boot_rows.get(key) for key in frozen):
            # A restart-required value changed: the live object keeps the booted
            # one, and cross-field rules must be judged against that.
            booted = {
                entry.key: state.effective[entry.key].value
                for entry in backed
                if entry.key in state.effective
            }
            effective_candidate = _in_force(booted).settings
            if effective_candidate is not None:
                candidate = effective_candidate
        broken = candidate.broken_rules()
        if broken:
            # Every write made here is refused when it would break a rule, so
            # only a row changed some other way gets here. Startup would refuse
            # this configuration; keep the one in force.
            logger.error(
                "Stored configuration not applied: it breaks a rule between settings (%s); "
                "keeping the values in force",
                "; ".join(broken),
            )
            return invalid
    targets = [
        entry.field
        for entry in backed
        if entry.field is not None and (boot or entry.key in state.live_keys)
    ]
    _copy_settings(candidate, [*targets, *registry.DERIVED_SETTINGS_FIELDS])
    state.overlaid = True
    if boot:
        state.applied_at_boot = frozenset(key for key in stored if key not in invalid)
    for key, message in invalid.items():
        logger.warning("Ignoring stored %s: %s", key, message)
    return invalid


# --- health ------------------------------------------------------------------


def _runtime_flag(key: str, fallback: bool) -> bool:
    """Read a boolean runtime setting from its in-memory cache, else *fallback*."""
    from serving.config.runtime_settings import get_runtime_settings_instance

    try:
        found, value = get_runtime_settings_instance().get_cached(key)
    except (RuntimeError, KeyError):
        return fallback
    return bool(value) if found else fallback


def _manifest_allows_signup() -> bool:
    from serving.auth.signup_policy import distribution_allows_public_signup
    from serving.config.distribution import DistributionConfigError

    try:
        return distribution_allows_public_signup()
    except (DistributionConfigError, ValidationError):
        # No manifest to read: a broken one, or an environment that does not
        # validate (a command-line tool can run on one).
        return False


def _current_settings() -> Settings:
    """Return the live settings, or the built-in defaults when the environment is invalid.

    Only a command-line tool runs on an environment that does not validate (the
    gateway does not start on one), and it must still list and change the
    stored settings; which settings are required then follows the defaults.
    """
    try:
        return get_settings()
    except ValidationError:
        return Settings.model_construct()


def _requirement_context() -> registry.RequirementContext:
    settings = _current_settings()
    signup_open = _manifest_allows_signup() and _runtime_flag(
        "signup_enabled", settings.signup_enabled
    )
    return registry.RequirementContext(
        database_enabled=_STATE.database_enabled,
        user_auth_enabled=_runtime_flag("user_auth_enabled", settings.user_auth_enabled),
        email_verification_needed=signup_open
        and _runtime_flag(
            "signup_require_email_verification", settings.signup_require_email_verification
        ),
        value=_configured,
    )


def _restart_pending(key: str, entry: ConfigEntry) -> bool:
    booted = _resolve(_STATE.boot_rows, key, entry)
    current = _resolve(_STATE.rows, key, entry)
    return _comparable(entry, booted) != _comparable(entry, current)


def _compute_health() -> ConfigHealth:
    context = _requirement_context()
    missing: list[str] = []
    pending: list[str] = []
    for key, entry in _STATE.entries.items():
        if entry.is_required(context) and not _configured(key).strip():
            missing.append(key)
        if entry.restart_required and _restart_pending(key, entry):
            pending.append(key)
    return ConfigHealth(missing=tuple(missing), pending_restart=tuple(pending))


def refresh_health() -> ConfigHealth:
    """Recompute :func:`get_config_health`, e.g. after a runtime setting changed."""
    try:
        _STATE.health = _compute_health()
    except Exception:
        logger.warning("Configuration health check failed", exc_info=True)
    return _STATE.health


# --- loading -----------------------------------------------------------------


def _discover() -> dict[str, DiscoveredReference]:
    """Scan the active models, routing and alerts files for ``${VAR}`` references."""
    from serving.config.distribution import resolve_config_path

    paths = {}
    for kind in ("models", "routing", "alerts"):
        try:
            paths[kind] = resolve_config_path(kind).path  # type: ignore[arg-type]
        except Exception:
            logger.warning("Cannot locate the %s file to scan for settings", kind, exc_info=True)
            paths[kind] = None
    return registry.discover_references(paths["models"], paths["routing"], paths["alerts"])


def _build_entries(rows: Mapping[str, ConfigRow]) -> dict[str, ConfigEntry]:
    return registry.build_entries(
        _STATE.discovered, {key: row.secret for key, row in rows.items()}, os.environ
    )


def _row_problem(key: str, row: ConfigRow) -> str | None:
    """Return why the registry refuses a stored row, if it does."""
    entry = _STATE.entries.get(key)
    return _value_problem(entry, row.value) if entry is not None else None


def _apply(rows: dict[str, ConfigRow], *, boot: bool) -> None:
    """Make *rows* the configuration in force, and tell everything that reads it."""
    state = _STATE
    state.generation += 1
    if boot:
        state.boot_rows = dict(rows)
    state.rows = rows
    state.entries = _build_entries(rows)
    state.live_keys = frozenset(
        key
        for key, entry in state.entries.items()
        if entry.origin == "static" and not entry.restart_required
    )
    effective = {key: row for key, row in rows.items() if key in state.live_keys}
    effective.update(
        (key, row) for key, row in state.boot_rows.items() if key not in state.live_keys
    )
    # A stored value the registry refuses (an environment import from before a
    # bound existed, a hand-edited row) is reported and left out, so the
    # environment or the default applies as if it had never been stored.
    refused = {key: problem for key, row in rows.items() if (problem := _row_problem(key, row))}
    state.effective = {key: row for key, row in effective.items() if not _row_problem(key, row)}
    try:
        state.invalid = {**refused, **_overlay_settings(boot=boot, refused=refused)}
    except Exception:
        state.invalid = refused
        logger.exception("Applying stored settings failed; keeping the previous values")
    for key, problem in refused.items():
        logger.warning("Ignoring stored %s: %s", key, problem)
    logging_utils.setup_logging()
    _notify()
    refresh_health()


async def load(
    store: AppConfigStore, *, database_enabled: bool = True, bootstrap: bool = True
) -> None:
    """Load the configuration, on a connection the caller owns.

    Creates the table and applies what is stored. At boot it first imports
    every registered setting that has a non-empty environment value and no row,
    and generates the secrets the deployment cannot start without.

    Args:
        store: The table, on the caller's connection.
        database_enabled: Whether the deployment has a database (it does here).
        bootstrap: Import the environment and generate secrets first. A
            command-line tool passes False: it reads and changes only what it
            is asked to, and must work where startup refuses to.

    Raises:
        ConfigBootstrapError: ``API_KEY_SECRET`` would have to be generated
            although API keys hashed with an earlier secret exist.
    """
    _STATE.database_enabled = database_enabled
    _STATE.discovered = _discover()
    await store.ensure_schema()
    rows = await store.fetch_all()
    if bootstrap:
        rows = await _import_environment(store, rows)
        rows = await _generate_secrets(store, rows)
    _STATE.loaded = True
    _apply(rows, boot=True)


async def _import_environment(
    store: AppConfigStore, rows: dict[str, ConfigRow]
) -> dict[str, ConfigRow]:
    """Store every registered setting the environment sets and the table lacks.

    A setting an administrator reset is left out by the store: it keeps
    following the environment instead of taking today's value for good. So is
    a value ``Settings`` cannot accept at all: stored, it would win over the
    environment once that was corrected, and could only ever be refused.

    The rules between settings are judged on the configuration in force, the
    table's values over the environment, not on the environment alone: a value
    whose rule a stored value satisfies is imported like any other. Importing
    a value changes nothing in force (it applies already, from the
    environment); it only keeps it once the variable is gone.
    """
    entries = _build_entries(rows)
    unusable = {registry.settings_env_name(name) for name in invalid_environment()}
    # Neither side of a rule the configuration in force breaks: startup refuses
    # it, which side is wrong is the operator's call, and a stored side would
    # outlive the correction.
    in_force = _in_force(_stored_settings(rows, entries))
    unusable.update(key for keys in in_force.broken.values() for key in keys)
    imports = []
    for key, entry in entries.items():
        value = _environ_value(key, entry)
        if entry.origin == "custom" or key in rows or key in unusable or not (value or "").strip():
            continue
        imports.append(
            ConfigRow(key, value, secret=entry.secret, source="env_import", updated_by="env-import")
        )
    imported = await store.insert_missing(imports)
    if not imported:
        return rows
    logger.info(
        "Imported %d setting(s) from the environment into the database: %s",
        len(imported),
        ", ".join(sorted(imported)),
    )
    return await store.fetch_all()


async def _generate_secrets(
    store: AppConfigStore, rows: dict[str, ConfigRow]
) -> dict[str, ConfigRow]:
    """Generate the secrets nothing has set: neither a row nor the environment.

    A secret an administrator reset to its environment value is generated
    again only once the environment no longer has one, and then replaces the
    reset row.

    Raises:
        ConfigBootstrapError: ``API_KEY_SECRET`` would have to be generated
            although API keys hashed with an earlier secret exist.
    """
    entries = _build_entries(rows)
    generated: list[ConfigRow] = []
    for key, entry in entries.items():
        if not entry.generated or key in rows or (_environ_value(key, entry) or "").strip():
            continue
        if key == "API_KEY_SECRET" and await store.api_keys_exist():
            raise ConfigBootstrapError(
                "API_KEY_SECRET is set neither in the database nor in the environment, but "
                "API keys issued under an earlier value exist; a new secret would invalidate "
                "every one of them. Restore the previous API_KEY_SECRET in the environment, or "
                "with `python -m serving.config.manage set API_KEY_SECRET <value>`, and "
                "restart."
            )
        generated.append(
            ConfigRow(
                key,
                secrets.token_urlsafe(_GENERATED_SECRET_BYTES),
                secret=True,
                source="generated",
            )
        )
    if not generated:
        return rows
    added = await store.insert_missing(generated, replace_reset=True)
    if added:
        logger.info("Generated %s into the database", ", ".join(sorted(added)))
    # Another worker may have won the race; re-read whichever value stuck.
    return await store.fetch_all()


def require_valid_environment() -> None:
    """Refuse to start on a configuration that is not valid once the database has been read.

    :func:`~serving.config.settings.get_settings` judges two things only here,
    called once the stored configuration is in force, instead of failing before
    the database could be read:

    - An environment value that is not valid on its own, left at its default:
      a valid stored value for the setting applies, and the gateway starts with
      a warning; without one it must not.
    - The rules between settings: the configuration in force, stored values
      over the environment, must satisfy every one.

    Raises:
        ConfigBootstrapError: Naming each such setting or broken rule, what is
            wrong, and how to fix it.
    """
    unresolved: dict[str, str] = {}
    for name, problem in invalid_environment().items():
        key = registry.settings_env_name(name)
        if key in _STATE.applied_at_boot:
            logger.warning(
                "Ignoring %s from the environment, which is not valid (%s): the value "
                "stored in the database applies",
                key,
                problem,
            )
        else:
            unresolved[key] = problem
    if not unresolved:
        _require_rules()
        return
    listed = "; ".join(f"{key}: {problem}" for key, problem in unresolved.items())
    remedy = "Fix or remove the variable"
    # Only a registered setting can be stored here; a runtime switch
    # (USER_AUTH_ENABLED, say) belongs to the Settings tab instead.
    storable = [key for key in unresolved if key in _STATE.entries]
    if _STATE.database_enabled and storable:
        remedy += f", or store a valid value with `python -m serving.config.manage set {storable[0]} VALUE`"
        if not _STATE.loaded:
            remedy += " once the database can be reached"
    raise ConfigBootstrapError(
        "The environment holds settings that are not valid, and the database has no "
        f"valid value in their place ({listed}). {remedy}, then start the gateway again."
    )


def _require_rules() -> None:
    """Refuse to start on a configuration in force that breaks a rule between settings."""
    broken = get_settings().broken_rules()
    if not broken:
        return
    keys = sorted(
        {registry.settings_env_name(name) for fields in broken.values() for name in fields}
    )
    remedy = "Correct " + ", ".join(keys)
    storable = [key for key in keys if key in _STATE.entries]
    if _STATE.database_enabled and storable:
        remedy += (
            " (a value stored in the database wins over the environment; change it with "
            "`python -m serving.config.manage set`"
        )
        remedy += " once the database can be reached)" if not _STATE.loaded else ")"
    raise ConfigBootstrapError(
        "The configuration in force, from the database and the environment together, "
        f"breaks a rule between settings ({'; '.join(broken)}). {remedy}, then start the "
        "gateway again."
    )


def use_environment(*, database_enabled: bool) -> None:
    """Run on the environment alone: database-free, or the database was unreachable."""
    _STATE.database_enabled = database_enabled
    _STATE.discovered = _discover()
    _apply({}, boot=True)


def attach(store: AppConfigStore) -> None:
    """Use *store* (pool-backed) for refreshes and administrator writes."""
    _STATE.store = store


async def refresh() -> None:
    """Re-read ``app_config`` and apply it when it changed."""
    store = _STATE.store
    if store is None:
        return
    if not _STATE.loaded:
        await store.ensure_schema()
    generation = _STATE.generation
    rows = await store.fetch_all()
    if generation != _STATE.generation:
        return  # A newer read was applied while this one ran.
    if not _STATE.loaded or rows != _STATE.rows:
        _STATE.loaded = True
        _apply(rows, boot=False)
    else:
        # Runtime settings (signup, verification) feed the required rules.
        refresh_health()


async def _refresh_loop(interval_seconds: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await refresh()
        except Exception:
            logger.warning("Configuration refresh failed", exc_info=True)


def start_refresh(interval_seconds: float = REFRESH_INTERVAL_SECONDS) -> None:
    """Start re-reading ``app_config`` every *interval_seconds* (needs :func:`attach`)."""
    if _STATE.store is None:
        return
    task = _STATE.refresh_task
    if task is not None and not task.done():
        return
    _STATE.refresh_task = asyncio.create_task(_refresh_loop(interval_seconds))


async def stop_refresh() -> None:
    """Stop the refresh loop started by :func:`start_refresh`."""
    task, _STATE.refresh_task = _STATE.refresh_task, None
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _reload(store: AppConfigStore) -> None:
    """Re-read and apply, so a write takes effect in this process at once."""
    while True:
        generation = _STATE.generation
        rows = await store.fetch_all()
        if generation == _STATE.generation:
            _STATE.loaded = True
            _apply(rows, boot=False)
            return


@contextlib.asynccontextmanager
async def command_line_session() -> AsyncIterator[None]:
    """Load the stored configuration for a command-line tool, on its own connection.

    Connects with the ``DB_*`` settings alone and loads read-only
    (``bootstrap=False``: no environment import, no generated secrets), so it
    works where startup refuses to, an invalid setting in the environment
    included. The connection serves :func:`update` and :func:`delete` until the
    block ends; what was loaded stays applied afterwards.
    """
    connection = await connect(DatabaseSettings())
    try:
        store = AppConfigStore(connection)
        await load(store, bootstrap=False)
        attach(store)
        yield
    finally:
        _STATE.store = None
        await connection.close()


def reset_state() -> None:
    """Forget everything loaded and return to the environment (tests)."""
    global _STATE
    if _STATE.generation == 0 and _STATE.store is None:
        return
    task = _STATE.refresh_task
    if task is not None:
        # The loop the task ran on may already be closed.
        with contextlib.suppress(RuntimeError):
            task.cancel()
    _STATE = _State()
    logging_utils.setup_logging()
    _notify()


# --- administration ----------------------------------------------------------


def _entry_for(key: str, rows: Mapping[str, ConfigRow]) -> ConfigEntry | None:
    entry = _build_entries(rows).get(key)
    return entry if entry is not None else registry.numbered_key_entry(key)


def _writable_entry(key: str, rows: Mapping[str, ConfigRow], *, new_secret: bool) -> ConfigEntry:
    if registry.is_environment_only(key):
        raise ConfigUpdateError(
            403,
            f"{key} stays in the environment ({registry.ENVIRONMENT_ONLY[key]}) and cannot "
            "be set here.",
        )
    if registry.is_runtime_setting_name(key):
        raise ConfigUpdateError(400, f"{key}: change it on the Settings tab.")
    entry = _entry_for(key, rows)
    if entry is not None:
        return entry
    if not registry.CUSTOM_KEY_RE.fullmatch(key):
        raise ConfigUpdateError(
            400, f"{key}: a name is an uppercase letter followed by A-Z, 0-9 or _."
        )
    return registry.custom_entry(key, secret=new_secret)


def _immutable_reason(entry: ConfigEntry, rows: Mapping[str, ConfigRow]) -> str | None:
    """Return why an immutable entry can no longer change, if it cannot."""
    if not entry.immutable:
        return None
    if (_resolve(rows, entry.key, entry) or "").strip():
        return f"{entry.key} is already set and cannot be changed."
    fallback = entry.fallback_key
    if fallback and (_resolve(rows, fallback, registry.static_entry(fallback)) or "").strip():
        return (
            f"{entry.key} cannot be set: the gateway already uses {fallback} in its place, "
            "and records depend on that value."
        )
    return None


def parse_text_value(key: str, text: str) -> Any:
    """Convert command-line text to the JSON value :func:`update` takes for *key*.

    A boolean is spelled as ``Settings`` spells one (``true``/``false``,
    ``1``/``0``, ``yes``/``no``, ``on``/``off``); a number as a number; anything
    else, including a name the registry does not know, stays text.

    Raises:
        ConfigUpdateError: 400 when *text* is not of the setting's type.
    """
    entry = _STATE.entries.get(key) or registry.numbered_key_entry(key)
    if entry is None or entry.type in ("str", "text", "list"):
        return text
    try:
        if entry.type == "bool":
            return _parse_bool(text)
        if entry.type == "int":
            return int(text.strip())
        return float(text.strip())
    except ValueError:
        noun = {"bool": "true or false", "int": "an integer", "float": "a number"}[entry.type]
        raise ConfigUpdateError(400, f"{key}: must be {noun}.") from None


def _stored_form(entry: ConfigEntry, value: Any) -> str:
    """Convert a JSON value from the admin API to the stored string form."""
    key = entry.key
    if value is None:
        raise ConfigUpdateError(400, f"{key}: a value is required; reset the setting to clear it.")
    if entry.type == "bool":
        if not isinstance(value, bool):
            raise ConfigUpdateError(400, f"{key}: must be true or false.")
        if entry.flag:
            return "1" if value else "0"
        return "true" if value else "false"
    if entry.type in ("int", "float"):
        # bool is an int to Python, and must not pass as one here.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            noun = "an integer" if entry.type == "int" else "a number"
            raise ConfigUpdateError(400, f"{key}: must be {noun}.")
        if not math.isfinite(value):
            raise ConfigUpdateError(400, f"{key}: must be a finite number.")
        if entry.type == "int":
            if isinstance(value, float) and not value.is_integer():
                raise ConfigUpdateError(400, f"{key}: must be an integer.")
            value = int(value)
        raw = str(value)
    elif isinstance(value, str):
        raw = value
    else:
        raise ConfigUpdateError(400, f"{key}: must be a string.")
    problem = _value_problem(entry, raw)
    if problem:
        raise ConfigUpdateError(400, f"{key}: {problem}.")
    return raw


@dataclass(frozen=True)
class _Damage:
    """How a write would leave the configuration in force worse than it is."""

    #: The written setting to name.
    key: str
    #: What would go wrong.
    reasons: tuple[str, ...]
    #: The other settings to change with it, if that is the remedy.
    others: tuple[str, ...] = ()

    def saving(self) -> str:
        """Describe it for a save: ``KEY: reason``, the admin API's form."""
        detail = f"{self.key}: {'; '.join(self.reasons)}"
        if self.others:
            detail += f"; change {', '.join(self.others)} in the same save"
        return detail

    def resetting(self) -> str:
        """Describe it for a reset of :attr:`key`."""
        detail = f"{self.key} cannot be reset: {'; '.join(self.reasons)}"
        if self.others:
            detail += f"; change {', '.join(self.others)} first"
        return detail


def _damage(
    rows: Mapping[str, ConfigRow], after: Mapping[str, ConfigRow], written: Sequence[str]
) -> _Damage | None:
    """Return how replacing *rows* with *after* would leave the configuration in force worse.

    Both are judged as the overlay applies them (:func:`_in_force`), so a write
    is refused exactly when what it puts in force would be worse than what is
    in force now, and a value or rule that is already wrong never blocks an
    unrelated change. Worse is any of:

    - a written value that cannot apply;
    - a stored value that applies now and would not;
    - a rule between settings broken that holds now;
    - an environment value that is not valid back in force (a reset of the
      stored value that replaces it).

    Args:
        rows: The table as it is.
        after: The table as the write would leave it.
        written: The keys the write stores or resets, in the request's order.
    """
    if not any(
        (entry := registry.static_entry(key)) is not None and entry.field is not None
        for key in written
    ):
        return None
    stored_now = _stored_settings(rows, _build_entries(rows))
    now = _in_force(stored_now)
    then = _in_force(_stored_settings(after, _build_entries(after)))

    def involved(keys: Iterable[str]) -> str:
        """The written key a rule ties, else the first written key."""
        return next((key for key in written if key in keys), written[0])

    for key in written:
        if key in then.refused:
            return _Damage(key, (then.refused[key],))
    if then.settings is None:
        if now.settings is None:
            return None
        return _Damage(written[0], ("the environment does not validate without it",))
    blamed: str | None = None
    reasons: list[str] = []
    others: list[str] = []
    knocked_out = [
        key
        for key in then.refused
        if key in stored_now and key not in now.refused and key not in written
    ]
    for key in knocked_out:
        reason = then.refused[key]
        blamed = blamed or involved(then.rules.get(reason, ()))
        reasons.append(reason)
        others.append(key)
    for rule, keys in then.broken.items():
        if rule in now.broken:
            continue
        blamed = blamed or involved(keys)
        reasons.append(rule)
        others.extend(key for key in keys if key not in written)
    if blamed is not None:
        return _Damage(blamed, tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(others)))
    for key, problem in then.set_aside.items():
        if key not in now.set_aside:
            return _Damage(
                key,
                (
                    f"its environment value is not valid ({problem}); correct or remove the "
                    "variable first",
                ),
            )
    return None


async def _writable_store() -> AppConfigStore:
    store = _STATE.store
    if store is None:
        raise ConfigUpdateError(
            503, "Configuration storage is unavailable: the database is not connected."
        )
    if not _STATE.loaded:
        await store.ensure_schema()
    return store


async def update(
    values: Mapping[str, Any],
    new_secrets: Mapping[str, bool] | None = None,
    *,
    updated_by: str,
) -> list[ConfigChange]:
    """Validate and store a batch of values, then apply it.

    Args:
        values: Setting name to its JSON value: a boolean for ``bool``, a number
            for ``int``/``float``, a string otherwise.
        new_secrets: The secret flag for custom variables this batch adds.
        updated_by: Identity of the administrator.

    Returns:
        One change per stored value, for the audit log.

    Raises:
        ConfigUpdateError: 400 for an invalid value, or for a batch that would
            leave the configuration in force worse (another stored value that
            could no longer apply, a rule between settings broken); 403 for an
            environment-only name, 409 for an immutable setting already set,
            503 without a database. Nothing is stored when any value fails.
    """
    if not values:
        return []
    store = await _writable_store()
    rows = await store.fetch_all()
    pending: list[ConfigRow] = []
    changes: list[ConfigChange] = []
    for key, value in values.items():
        is_new = key not in rows
        entry = _writable_entry(key, rows, new_secret=bool((new_secrets or {}).get(key)) and is_new)
        reason = _immutable_reason(entry, rows)
        if reason is not None:
            raise ConfigUpdateError(409, reason)
        raw = _stored_form(entry, value)
        secret = entry.secret or (not is_new and rows[key].secret)
        pending.append(ConfigRow(key, raw, secret=secret, source="admin", updated_by=updated_by))
        changes.append(
            ConfigChange(
                key,
                secret,
                old_value=None if secret else _typed(entry, _resolve(rows, key, entry)),
                new_value=None if secret else _typed(entry, raw),
            )
        )
    damage = _damage(rows, {**rows, **{row.key: row for row in pending}}, list(values))
    if damage is not None:
        raise ConfigUpdateError(400, damage.saving())
    await store.write(pending)
    await _reload(store)
    return changes


async def delete(key: str, *, updated_by: str) -> ConfigChange:
    """Reset *key*: it follows the environment, then its default, from now on.

    The stored value is replaced by a reset row, which keeps the next boot from
    importing the environment value as if it had been set here, so a later
    change to the environment still applies. A custom variable is removed.

    Raises:
        ConfigUpdateError: 403 for an environment-only name, 409 for an
            immutable setting, a generated secret with nothing to fall back
            to, or a reset that would leave the configuration in force worse
            (a stored value that depends on this one, an environment value that
            is not valid), 404 when nothing is stored, 503 without a database.
    """
    if registry.is_environment_only(key):
        raise ConfigUpdateError(
            403, f"{key} stays in the environment ({registry.ENVIRONMENT_ONLY[key]})."
        )
    store = await _writable_store()
    rows = await store.fetch_all()
    entry = _entry_for(key, rows)
    if entry is not None and entry.immutable:
        raise ConfigUpdateError(409, f"{key} cannot be changed.")
    row = rows.get(key)
    if row is None or entry is None:
        raise ConfigUpdateError(404, f"{key} has no stored value.")
    if entry.generated and not (_environ_value(key, entry) or "").strip():
        raise ConfigUpdateError(
            409, f"{key} has no environment value to fall back to; replace it instead."
        )
    secret = entry.secret or row.secret
    remaining = {name: stored for name, stored in rows.items() if name != key}
    damage = _damage(rows, remaining, [key])
    if damage is not None:
        raise ConfigUpdateError(409, damage.resetting())
    change = ConfigChange(
        key,
        secret,
        old_value=None if secret else _typed(entry, row.value),
        new_value=None if secret else _typed(entry, _resolve(remaining, key, entry)),
    )
    if entry.origin == "custom":
        await store.delete(key)
    else:
        await store.reset(key, secret=secret, updated_by=updated_by)
    await _reload(store)
    logger.info("Configuration %s reset by %s", key, updated_by)
    return change


def describe() -> dict[str, Any]:
    """Return every entry as the admin API shows it; secrets carry no value."""
    state = _STATE
    health = refresh_health()
    context = _requirement_context()
    missing = set(health.missing)
    pending = set(health.pending_restart)
    entries: list[dict[str, Any]] = []
    for key, entry in state.entries.items():
        row = state.rows.get(key)
        environ_value = _environ_value(key, entry)
        if row is not None:
            raw, source = row.value, "database"
        elif environ_value is not None:
            raw, source = environ_value, "environment"
        else:
            raw, source = entry.default, "default"
        secret = entry.secret or (row is not None and row.secret)
        invalid = state.invalid.get(key)
        if invalid is None and row is not None:
            invalid = _value_problem(entry, row.value)
        entries.append(
            {
                "key": key,
                "category": entry.category,
                "description": entry.description,
                "type": entry.type,
                "secret": secret,
                "required": entry.is_required(context),
                "missing": key in missing,
                "is_set": bool(raw and raw.strip()),
                "value": None if secret else _typed(entry, raw),
                "default": None if secret else _typed(entry, entry.default),
                "source": source,
                "restart_required": entry.restart_required,
                "pending_restart": key in pending,
                # Compared as typed values: an environment "1" and a stored
                # "true" are the same setting, not one overriding the other.
                "environment_ignored": row is not None
                and bool(environ_value and environ_value.strip())
                and _comparable(entry, environ_value) != _comparable(entry, row.value),
                "immutable": entry.immutable,
                "setup": entry.setup,
                "custom": entry.origin == "custom",
                "invalid": invalid,
                "used_by": list(entry.used_by),
                "updated_at": row.updated_at if row is not None else None,
                "updated_by": row.updated_by if row is not None else None,
            }
        )
    return {
        "categories": [
            {"id": category.id, "label": category.label, "description": category.description}
            for category in registry.CATEGORIES
        ],
        "entries": entries,
        "missing": list(health.missing),
        "pending_restart": list(health.pending_restart),
    }
