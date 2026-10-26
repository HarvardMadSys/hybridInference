"""The settings that database-backed configuration manages.

One :class:`ConfigEntry` per setting, keyed by its canonical environment-variable
name. Entries come from four places:

1. **Static** — every :class:`~serving.config.settings.Settings` field and every
   variable the backend reads directly, except the environment-only names in
   :data:`ENVIRONMENT_ONLY` and the runtime settings, which live in
   ``site_settings`` and have their own admin tab.
2. **Discovered** — every ``${VAR}`` reference in the active model registry,
   routing file and alert rules (:func:`discover_references`).
3. **Numbered provider keys** — ``<KEY>1`` … ``<KEY>20`` beside each provider
   key the key pools scan, listed when the database or the environment has one.
4. **Custom** — any other well-formed name an administrator adds for a
   reference the registry cannot see.

``tests/unit/config/test_app_config_registry.py`` fails when a ``Settings``
field is in none of the static entries, :data:`ENVIRONMENT_ONLY`, the runtime
settings or :data:`UNREGISTERED_SETTINGS_FIELDS`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

import yaml

from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY

# settings_init_name is re-exported: callers map a registry entry's field to
# its Settings keyword through this module.
from serving.config.settings import (
    Settings,
    settings_env_name,
    settings_init_name as settings_init_name,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

ValueType = Literal["str", "text", "int", "float", "bool", "list"]
Origin = Literal["static", "discovered", "numbered", "custom"]


@dataclass(frozen=True)
class Category:
    """A group of entries in the admin Configuration tab."""

    id: str
    label: str
    description: str
    #: Whether its settings can take effect in this deployment; the console
    #: offers none of them while it returns false. ``None`` is always.
    in_effect: Callable[[], bool] | None = None


def _sms_provider_registered() -> bool:
    # Phone sign-in needs a provider an extension registers; "none" is always listed.
    from serving.utils.delivery import sms_provider_names

    return len(sms_provider_names()) > 1


CATEGORIES: tuple[Category, ...] = (
    Category("general", "General", "Public addresses, site identity and logging."),
    Category(
        "security",
        "Security",
        "Signing secrets, administrator access, sessions and abuse protection.",
    ),
    Category(
        "signup",
        "Sign-up",
        "Who hears about new accounts, how fast they may register, and the signup captcha.",
    ),
    Category(
        "email",
        "Email",
        "Outgoing mail for verification, password reset and notifications.",
    ),
    Category(
        "phone",
        "Phone authentication",
        "Optional phone sign-in and SMS delivery.",
        in_effect=_sms_provider_registered,
    ),
    Category(
        "providers",
        "Providers",
        "Credentials and endpoints for upstream model providers, including the variables "
        "the model registry references.",
    ),
    Category(
        "routing",
        "Routing",
        "Route selection, endpoint health, timeouts and outbound concurrency.",
    ),
    Category(
        "batch",
        "Batch inference",
        "Who may submit batches, how much of each model they may use, and how long jobs "
        "and results are kept.",
    ),
    Category(
        "network", "Network", "Browser origins and which proxies may report client addresses."
    ),
    Category("alerts", "Alerts", "Backend alert notifications."),
    Category(
        "integrations",
        "Integrations",
        "Cloud agent identity, the documentation assistant and Qdrant.",
    ),
    Category("privacy", "Privacy", "Request content storage and account deletion."),
)

CATEGORY_IDS: frozenset[str] = frozenset(category.id for category in CATEGORIES)
_CATEGORY_BY_ID: dict[str, Category] = {category.id: category for category in CATEGORIES}


@dataclass(frozen=True)
class RequirementContext:
    """What a ``required`` predicate may consult about the effective configuration."""

    database_enabled: bool
    user_auth_enabled: bool
    #: Public signup is open and new accounts must verify their email.
    email_verification_needed: bool
    #: Configured value of a key — database row, environment, default — or "".
    value: Callable[[str], str]
    #: The active transport can send mail, including one installed by an extension.
    email_enabled: bool = False


Requirement = bool | Callable[[RequirementContext], bool]


@dataclass(frozen=True)
class ConfigEntry:
    """One setting the database-backed configuration manages."""

    key: str
    category: str
    description: str
    type: ValueType = "str"
    #: The ``Settings`` attribute this entry overlays; ``None`` for values read
    #: through ``config_value()``.
    field: str | None = None
    #: Built-in default in environment-string form; ``None`` when there is none.
    default: str | None = None
    secret: bool = False
    required: Requirement = False
    #: Captured at startup, so a change applies after a restart.
    restart_required: bool = False
    #: Shown on the first-run configuration step.
    setup: bool = False
    #: Cannot be changed once set.
    immutable: bool = False
    #: Generated on first boot when neither the database nor the environment has it.
    generated: bool = False
    origin: Origin = "static"
    #: Model ids whose configuration references the variable (discovered entries).
    used_by: tuple[str, ...] = ()
    #: Accepted values, compared case-insensitively unless ``case_sensitive``.
    #: An entry with choices cannot be stored empty.
    choices: tuple[str, ...] = ()
    #: Compare ``choices`` exactly, for a value passed on without folding case.
    case_sensitive: bool = False
    minimum: float | None = None
    maximum: float | None = None
    #: A switch read as ``value != "0"``: stored as ``1``/``0``, not ``true``/``false``.
    flag: bool = False
    #: The key whose value applies while this one is empty.
    fallback_key: str | None = None

    def is_required(self, context: RequirementContext) -> bool:
        """Evaluate ``required`` against the effective configuration."""
        if callable(self.required):
            return bool(self.required(context))
        return self.required


#: Spellings ``Settings`` reads as true and as false.
TRUE_WORDS = frozenset({"1", "on", "t", "true", "y", "yes"})
FALSE_WORDS = frozenset({"0", "off", "f", "false", "n", "no"})


def parse_bool(raw: str) -> bool:
    """Parse a boolean the way ``Settings`` does.

    Raises:
        ValueError: *raw* is not a boolean spelling.
    """
    word = raw.strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ValueError(raw)


def _auth_secret_required(context: RequirementContext) -> bool:
    # Mirrors Settings.validate_auth_secrets: only a database-free gateway with
    # user authentication off runs without these.
    return context.database_enabled or context.user_auth_enabled


def _smtp_required(context: RequirementContext) -> bool:
    # Without mail, a new account can never verify its address.
    return (
        context.value("EMAIL_PROVIDER") in ("", "smtp")
        and context.email_verification_needed
        and not context.email_enabled
    )


def _env_string(value: Any) -> str | None:
    """Render a Python default in the form the environment variable would take."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def _setting(
    field_name: str,
    category: str,
    description: str,
    type: ValueType,
    **options: Any,
) -> ConfigEntry:
    """Build the entry for a ``Settings`` field, defaulting to the field's own default."""
    info = Settings.model_fields[field_name]
    return ConfigEntry(
        key=settings_env_name(field_name),
        category=category,
        description=description,
        type=type,
        field=field_name,
        default=_env_string(info.get_default(call_default_factory=True)),
        **options,
    )


# Static entries, grouped by category in the order the console lists them.
# Descriptions are operator-facing; they come from the comments in settings.py
# and .env.example.
_STATIC_ENTRIES: tuple[ConfigEntry, ...] = (
    # --- general -----------------------------------------------------------
    _setting(
        "base_url",
        "general",
        "Public URL of this backend API, used as the origin for outgoing links. Left "
        "blank, the backend derives it from each request, which yields plain http "
        "behind a TLS-terminating proxy; set it on any HTTPS deployment.",
        "str",
        setup=True,
    ),
    _setting(
        "frontend_url",
        "general",
        "Public URL of the console, used in email links (verification, password reset, "
        "approval). Must be absolute and reachable by your users.",
        "str",
        setup=True,
    ),
    ConfigEntry(
        "SITE_NAME",
        "general",
        "Name rendered into emails, broadcast templates and the docs assistant. Falls "
        "back to the active distribution manifest, then to HybridInference.",
        setup=True,
    ),
    ConfigEntry(
        "SITE_PUBLIC_BASE_URL",
        "general",
        "Public site URL rendered into backend-produced content. Falls back to the "
        "active distribution manifest.",
    ),
    ConfigEntry(
        "SITE_SUPPORT_EMAIL",
        "general",
        "Support address rendered into emails and help text. Falls back to the active "
        "distribution manifest.",
        setup=True,
    ),
    ConfigEntry(
        "SITE_DOCS_URL",
        "general",
        "HTTPS documentation URL rendered into backend-produced content. Overrides the "
        "manifest's branding.",
    ),
    ConfigEntry(
        "LOG_LEVEL",
        "general",
        "Log level. DEBUG also shows every access-log line.",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    ),
    ConfigEntry(
        "LOG_FORMAT",
        "general",
        "Log line format: json writes one JSON object per line; plain (or text) writes plain text.",
        default="plain",
        # The logger treats every value but "json" as plain text; "text" is
        # the spelling older deployments and the installation guide use.
        choices=("plain", "text", "json"),
    ),
    # --- security ----------------------------------------------------------
    _setting(
        "jwt_secret_key",
        "security",
        "Signs console session tokens. Generated on first boot; changing it signs every user out.",
        "str",
        secret=True,
        required=_auth_secret_required,
        generated=True,
    ),
    _setting(
        "api_key_secret",
        "security",
        "Hashes and encrypts user API keys. Generated on first boot. It cannot be "
        "changed: a new value would invalidate every API key.",
        "str",
        secret=True,
        required=_auth_secret_required,
        generated=True,
        immutable=True,
    ),
    _setting(
        "admin_token",
        "security",
        "Optional legacy bearer token for the admin API. Blank disables only this login path.",
        "str",
        secret=True,
    ),
    _setting(
        "admin_emails",
        "security",
        "Comma-separated addresses that are administrators when they sign up.",
        "list",
    ),
    # The bounds below keep one stored value from signing every user out: the
    # tokens are HMAC-signed with JWT_SECRET_KEY, and a lifetime past what a
    # timedelta holds fails every sign-in.
    _setting(
        "jwt_algorithm",
        "security",
        "HMAC algorithm that signs session tokens.",
        "str",
        choices=("HS256", "HS384", "HS512"),
        case_sensitive=True,
    ),
    _setting(
        "jwt_access_token_expire_minutes",
        "security",
        "Lifetime of a console access token, in minutes (at most 30 days).",
        "int",
        minimum=1,
        maximum=43_200,
    ),
    _setting(
        "jwt_refresh_token_expire_days",
        "security",
        "Lifetime of a console refresh token, in days (at most ten years).",
        "int",
        minimum=1,
        maximum=3650,
    ),
    _setting(
        "cookie_secure",
        "security",
        "Mark the refresh-token cookie Secure (sent over HTTPS only). Turn it off only "
        "for a plain-http local run.",
        "bool",
    ),
    _setting(
        "cookie_domain",
        "security",
        "Domain attribute of the refresh-token cookie. Empty for a host-only cookie.",
        "str",
    ),
    _setting(
        "cookie_samesite",
        "security",
        "SameSite attribute of the refresh-token cookie.",
        "str",
        choices=("lax", "strict", "none"),
    ),
    _setting(
        "login_rate_limit_per_15min",
        "security",
        "Sign-in attempts allowed per email address or username from one client address in "
        "15 minutes (IPv6 is bucketed to /64).",
        "int",
        minimum=1,
    ),
    _setting(
        "login_account_rate_limit_per_15min",
        "security",
        "Sign-in attempts allowed per email address or username from all client addresses "
        "together in 15 minutes, bounding password guessing spread across many addresses. "
        "Only attempts that are let through count, so each address adds at most its "
        "per-address allowance.",
        "int",
        minimum=1,
    ),
    _setting(
        "login_rate_limit_per_hour_per_ip",
        "security",
        "Sign-in attempts allowed per client address in an hour.",
        "int",
        minimum=1,
    ),
    _setting(
        "auth_failure_block_enabled",
        "security",
        "Block a client address after repeated API-key authentication failures.",
        "bool",
    ),
    _setting(
        "auth_failure_block_threshold",
        "security",
        "Failures within the window that block an address (IPv6 is bucketed to /64).",
        "int",
        minimum=1,
    ),
    _setting(
        "auth_failure_block_window_sec",
        "security",
        "Window, in seconds, over which authentication failures are counted.",
        "int",
        minimum=1,
    ),
    _setting(
        "auth_failure_block_duration_sec",
        "security",
        "How long, in seconds, a blocked address stays blocked.",
        "int",
        minimum=1,
    ),
    _setting(
        "unresolved_auth_failure_block_threshold",
        "security",
        "Unresolved authentication failures within the window that block unresolved traffic.",
        "int",
        minimum=1,
    ),
    _setting(
        "unresolved_auth_failure_block_window_sec",
        "security",
        "Window, in seconds, over which unresolved authentication failures are counted.",
        "int",
        minimum=1,
    ),
    _setting(
        "unresolved_auth_failure_block_duration_sec",
        "security",
        "How long, in seconds, unresolved traffic stays blocked.",
        "int",
        minimum=1,
    ),
    _setting(
        "auth_failure_block_exempt_ips",
        "security",
        "Comma-separated addresses or CIDR ranges never blocked, such as a campus NAT "
        "or office gateway shared by many clients.",
        "list",
    ),
    _setting(
        "auth_failure_identify_caller",
        "security",
        "Look up whose API key was rejected, so authentication-failure logs and alerts "
        "can name the account. Costs one indexed query per failure.",
        "bool",
    ),
    # --- signup ------------------------------------------------------------
    _setting(
        "signup_notify_emails",
        "signup",
        "Comma-separated recipients of new-registration approval emails. Empty sends "
        "them to ADMIN_EMAILS.",
        "list",
    ),
    _setting(
        "signup_default_daily_quota_usd",
        "signup",
        "Deprecated: daily USD quota for new users, used only when no per-role quota "
        "runtime setting applies.",
        "float",
        minimum=0,
    ),
    _setting(
        "signup_rate_limit_per_hour",
        "signup",
        "Signups allowed per client address in an hour.",
        "int",
        minimum=1,
    ),
    _setting(
        "signup_rate_limit_per_day",
        "signup",
        "Signups allowed per client address in a day.",
        "int",
        minimum=1,
    ),
    _setting(
        "signup_require_resolved_client_ip",
        "signup",
        "Reject signup when no client address can be resolved.",
        "bool",
    ),
    _setting(
        "unresolved_signup_rate_limit_per_hour",
        "signup",
        "Signups allowed per hour when no client address can be resolved.",
        "int",
        minimum=1,
    ),
    _setting(
        "unresolved_signup_rate_limit_per_day",
        "signup",
        "Signups allowed per day when no client address can be resolved.",
        "int",
        minimum=1,
    ),
    _setting(
        "turnstile_secret_key",
        "signup",
        "Cloudflare Turnstile secret for the signup captcha. Empty turns verification off.",
        "str",
        secret=True,
    ),
    # --- phone authentication -----------------------------------------------
    _setting(
        "sms_auth_enabled",
        "phone",
        "Enable phone registration, login and binding when SMS delivery is configured.",
        "bool",
    ),
    _setting(
        "sms_provider",
        "phone",
        "SMS verification provider. None disables delivery.",
        "str",
        choices=("none",),
        case_sensitive=True,
    ),
    _setting(
        "sms_default_country_code",
        "phone",
        "Optional country calling code for local input, without +; empty requires international format.",
        "str",
    ),
    _setting(
        "sms_allowed_prefixes",
        "phone",
        "Comma-separated allowed international prefixes, such as +86. Empty permits any prefix supported by the provider.",
        "str",
    ),
    _setting(
        "sms_otp_expire_seconds",
        "phone",
        "Verification code lifetime in seconds.",
        "int",
        minimum=60,
        maximum=600,
    ),
    _setting(
        "sms_otp_send_cooldown_seconds",
        "phone",
        "Minimum seconds between messages to one phone.",
        "int",
        minimum=60,
    ),
    _setting(
        "sms_otp_send_per_phone_hour", "phone", "Messages to one phone per hour.", "int", minimum=1
    ),
    _setting(
        "sms_otp_send_per_phone_day", "phone", "Messages to one phone per day.", "int", minimum=1
    ),
    _setting(
        "sms_otp_send_per_ip_hour",
        "phone",
        "Messages requested by one client address per hour.",
        "int",
        minimum=1,
    ),
    _setting(
        "sms_otp_send_per_ip_day",
        "phone",
        "Messages requested by one client address per day.",
        "int",
        minimum=1,
    ),
    _setting(
        "sms_otp_max_attempts",
        "phone",
        "Maximum guesses for a verification code.",
        "int",
        minimum=1,
        maximum=10,
    ),
    _setting(
        "sms_otp_verify_per_phone_15min",
        "phone",
        "Verification attempts for one phone per fifteen minutes.",
        "int",
        minimum=1,
    ),
    _setting(
        "sms_otp_verify_per_ip_hour",
        "phone",
        "Verification attempts from one client address per hour.",
        "int",
        minimum=1,
    ),
    # --- email -------------------------------------------------------------
    _setting(
        "email_provider",
        "email",
        "Outgoing email transport; SMTP preserves existing behavior.",
        "str",
        choices=("smtp",),
        case_sensitive=True,
        setup=True,
    ),
    _setting("smtp_host", "email", "SMTP server host.", "str", setup=True),
    _setting(
        "smtp_port", "email", "SMTP server port.", "int", setup=True, minimum=1, maximum=65_535
    ),
    _setting(
        "smtp_user",
        "email",
        "SMTP user name. Required while public signup requires email verification.",
        "str",
        setup=True,
        required=_smtp_required,
    ),
    _setting(
        "smtp_password",
        "email",
        "SMTP password. Required while public signup requires email verification.",
        "str",
        secret=True,
        setup=True,
        required=_smtp_required,
    ),
    _setting("smtp_from_email", "email", "Sender address of outgoing mail.", "str", setup=True),
    _setting("smtp_from_name", "email", "Sender name of outgoing mail.", "str", setup=True),
    # --- providers ---------------------------------------------------------
    _setting(
        "provider_route_types",
        "providers",
        "Route types the admin console may add per provider, as comma-separated "
        "provider=type[|type] entries (for example chutes=quota,openrouter=concurrency|"
        "on_demand). Unlisted providers may use any type.",
        "str",
    ),
    ConfigEntry(
        "CHUTES_BASE_URL",
        "providers",
        "Default base URL offered for new Chutes routes.",
        default="https://llm.chutes.ai/v1",
    ),
    ConfigEntry(
        "FEATHERLESS_BASE_URL",
        "providers",
        "Default base URL offered for new Featherless routes.",
        default="https://api.featherless.ai/v1",
    ),
    ConfigEntry(
        "MINIMAX_BASE_URL",
        "providers",
        "Default base URL offered for new MiniMax routes.",
        default="https://api.minimax.io/v1",
    ),
    ConfigEntry(
        "LOCAL_API_KEY",
        "providers",
        "Shared secret for the local inference proxies. Also the documentation "
        "assistant's ingest key when RAG_GATEWAY_API_KEY is empty.",
        secret=True,
        restart_required=True,
    ),
    # --- routing -----------------------------------------------------------
    _setting(
        "enable_routewise",
        "routing",
        "Make RouteWise the default router for every model that does not name one.",
        "bool",
        restart_required=True,
    ),
    _setting(
        "upstream_concurrency_enabled",
        "routing",
        "Cap this gateway's outbound concurrency per remote account, lowering it on "
        "every upstream 429 and probing back up after successes.",
        "bool",
        restart_required=True,
    ),
    _setting(
        "upstream_concurrency_initial_limit",
        "routing",
        "Starting outbound concurrency per remote account.",
        "int",
        restart_required=True,
    ),
    _setting(
        "upstream_concurrency_max_limit",
        "routing",
        "Highest outbound concurrency per remote account.",
        "int",
        restart_required=True,
    ),
    _setting(
        "upstream_concurrency_probe_success_interval",
        "routing",
        "Successful (HTTP 200) responses between upward probes of the limit.",
        "int",
        restart_required=True,
    ),
    _setting(
        "upstream_concurrency_acquire_timeout_sec",
        "routing",
        "Seconds a request waits for an outbound slot before failing over. Keep it well "
        "under the client request timeout.",
        "float",
        restart_required=True,
    ),
    ConfigEntry(
        "ROUTER_HEALTH_EWMA_ALPHA",
        "routing",
        "Smoothing factor of each endpoint's availability average.",
        type="float",
        default="0.1",
        restart_required=True,
        minimum=0,
        maximum=1,
    ),
    ConfigEntry(
        "CIRCUIT_FAILURE_THRESHOLD",
        "routing",
        "Consecutive failures that open an endpoint's circuit breaker.",
        type="int",
        default="3",
        restart_required=True,
        minimum=1,
    ),
    ConfigEntry(
        "CIRCUIT_COOLDOWN_SECONDS",
        "routing",
        "Seconds an open circuit waits before letting a probe through.",
        type="float",
        default="30",
        restart_required=True,
        minimum=0,
    ),
    ConfigEntry(
        "CIRCUIT_MIN_AVAILABILITY",
        "routing",
        "Availability below which an endpoint's circuit opens.",
        type="float",
        default="0.7",
        restart_required=True,
        minimum=0,
        maximum=1,
    ),
    ConfigEntry(
        "ROUTING_AFFINITY_ENABLED",
        "routing",
        "Keep a caller on the endpoint that already holds its prompt cache.",
        type="bool",
        default="1",
        flag=True,
    ),
    ConfigEntry(
        "ROUTING_AFFINITY_MAX_AGE_SEC",
        "routing",
        "Longest a caller stays pinned to one endpoint, in seconds. 0 removes the cap.",
        type="int",
        default="86400",
        minimum=0,
    ),
    ConfigEntry(
        "ROUTING_PREFILL_AWARE_ENABLED",
        "routing",
        "Steer requests away from endpoints busy with large prompt prefills.",
        type="bool",
        default="1",
        flag=True,
    ),
    ConfigEntry(
        "ROUTING_PREFILL_ELEPHANT_TOKENS",
        "routing",
        "Un-cached prompt tokens at which a request counts as a very large prefill.",
        type="int",
        default="200000",
        minimum=0,
    ),
    ConfigEntry(
        "ROUTING_PREFILL_ELEPHANT_LIMIT",
        "routing",
        "Very large prefills one endpoint may run at once.",
        type="int",
        default="1",
        minimum=0,
    ),
    ConfigEntry(
        "ROUTING_PREFILL_INTERVENE_TOKENS",
        "routing",
        "In-flight prefill backlog at which load starts overriding the weighted choice.",
        type="int",
        default="50000",
        minimum=0,
    ),
    ConfigEntry(
        "ROUTING_PREFILL_AFFINITY_CEILING",
        "routing",
        "Backlog above which a caller's endpoint pin is ignored for one request.",
        type="int",
        default="150000",
        minimum=0,
    ),
    ConfigEntry(
        "ROUTING_PREFILL_HINT_TTL_SEC",
        "routing",
        "Seconds a remembered prompt size discounts a warm continuation. Keep it below "
        "the local backends' idle stop.",
        type="int",
        default="1200",
        minimum=0,
    ),
    ConfigEntry(
        "ROUTING_PRIORITY_INTERACTIVE",
        "routing",
        "sglang scheduling priority of ordinary requests.",
        type="int",
        default="20",
        minimum=0,
    ),
    ConfigEntry(
        "ROUTING_PRIORITY_LARGE",
        "routing",
        "sglang scheduling priority of large prompts.",
        type="int",
        default="15",
        minimum=0,
    ),
    ConfigEntry(
        "ROUTING_PRIORITY_ELEPHANT",
        "routing",
        "sglang scheduling priority of very large prompts.",
        type="int",
        default="0",
        minimum=0,
    ),
    ConfigEntry(
        "UPSTREAM_COMPLETION_TIMEOUT_S",
        "routing",
        "Total seconds allowed for a non-streaming upstream completion.",
        type="float",
        default="600",
        minimum=1,
    ),
    ConfigEntry(
        "STREAM_IDLE_TIMEOUT_SECONDS",
        "routing",
        "Seconds an upstream stream may stay silent after its first frame. 0 or less "
        "turns the check off.",
        type="float",
        default="180",
    ),
    ConfigEntry(
        "STREAM_FIRST_BYTE_TIMEOUT_SECONDS",
        "routing",
        "Seconds to wait for an upstream stream's first frame. Empty or 0 waits without a limit.",
        type="float",
    ),
    ConfigEntry(
        "STREAM_MAX_IDLE_S",
        "routing",
        "Seconds the Anthropic Messages endpoint waits between upstream frames.",
        type="int",
        default="240",
        minimum=1,
    ),
    ConfigEntry(
        "STREAM_MAX_FIRST_FRAME_IDLE_S",
        "routing",
        "Seconds the Anthropic Messages endpoint waits for the first upstream frame. "
        "Never less than STREAM_MAX_IDLE_S.",
        type="int",
        default="300",
        minimum=1,
    ),
    ConfigEntry(
        "SMALL_MAXTOK_REASONING_TARGET",
        "routing",
        "Model that serves tiny-max_tokens Messages calls aimed at a reasoning model, "
        "when that reroute is switched on.",
        default="qwen3.6-35b",
    ),
    ConfigEntry(
        "SMALL_MAXTOK_REASONING_THRESHOLD",
        "routing",
        "max_tokens at or below which a Messages call counts as tiny. 0 turns the reroute off.",
        type="int",
        default="64",
        minimum=0,
    ),
    ConfigEntry(
        "SMALL_MAXTOK_REASONING_FLOOR",
        "routing",
        "Output budget a rerouted tiny call receives.",
        type="int",
        default="512",
        minimum=1,
    ),
    ConfigEntry(
        "REQUEST_TIMEOUT_SECONDS",
        "routing",
        "Seconds before a request that has not started responding gets a 504.",
        type="float",
        default="120",
        minimum=1,
    ),
    ConfigEntry(
        "STREAM_REQUEST_TIMEOUT_SECONDS",
        "routing",
        "Total seconds a streaming response may run. 0 or less removes the cap.",
        type="float",
        default="3600",
    ),
    # --- batch inference ---------------------------------------------------
    # Read through serving.batch.config, which falls back to the same defaults
    # and bounds; tests/unit/config/test_app_config_registry.py holds the two
    # to each other.
    ConfigEntry(
        "BATCH_ENABLED",
        "batch",
        "Serve batch inference (/v1/batches) and run its worker. Off, every batch route "
        "answers 404 and no queued request starts; stored batches are kept.",
        type="bool",
        default="1",
        flag=True,
    ),
    ConfigEntry(
        "BATCH_MIN_ROLE",
        "batch",
        "Lowest role that may submit batches. A queued request starts only while its "
        "owner still holds this role; owners can always read, cancel and delete their "
        "own batches.",
        default="internal",
        choices=("free", "pro", "internal", "admin"),
    ),
    ConfigEntry(
        "BATCH_MODEL_CONCURRENCY",
        "batch",
        "Batch requests a model runs at once, across the whole deployment, when it has "
        "no cap of its own. Per-model caps are set on the admin Settings page, under "
        "Batch Concurrency; this is only the default. 0 pauses batch work on every "
        "model without a cap.",
        type="int",
        default="64",
        minimum=0,
    ),
    ConfigEntry(
        "BATCH_YIELD_TO_INTERACTIVE",
        "batch",
        "Count interactive requests in flight on a model against its batch cap, so "
        "batch work backs off while the model is busy.",
        type="bool",
        default="1",
        flag=True,
    ),
    ConfigEntry(
        "BATCH_MAX_REQUESTS",
        "batch",
        "Requests one batch may hold.",
        type="int",
        default="50000",
        minimum=1,
    ),
    ConfigEntry(
        "BATCH_MAX_BODY_MB",
        "batch",
        "Largest batch submission, in MiB. A larger request body is refused.",
        type="int",
        default="32",
        minimum=1,
    ),
    ConfigEntry(
        "BATCH_MAX_ACTIVE_PER_USER",
        "batch",
        "Batches one user may have queued or running at once. Another is refused until "
        "one finishes.",
        type="int",
        default="10",
        minimum=1,
    ),
    ConfigEntry(
        "BATCH_COMPLETION_WINDOW_HOURS",
        "batch",
        "Hours a new batch has to finish, at most a week. Requests not started by then "
        "expire; those already running finish.",
        type="int",
        default="24",
        minimum=1,
        maximum=168,
    ),
    ConfigEntry(
        "BATCH_RETENTION_DAYS",
        "batch",
        "Days a finished batch and its results are kept before they are deleted. A "
        "change applies to batches created afterwards.",
        type="int",
        default="7",
        minimum=1,
    ),
    ConfigEntry(
        "BATCH_EXPANSION_ENABLED",
        "batch",
        "Allow creating the newer batch kinds: Anthropic Message Batches, file batches "
        "with the Files upload, and Embeddings and Responses requests. Turn on only once "
        "every gateway process runs a version that supports them. Off, batches already "
        "accepted still run and can still be read, downloaded, cancelled and deleted.",
        type="bool",
        default="0",
        flag=True,
    ),
    ConfigEntry(
        "BATCH_SHARED_INTERACTIVE_LOAD",
        "batch",
        "Count the interactive requests in flight on every gateway process, not only "
        "this one, when batch work yields to interactive traffic. If that shared count "
        "cannot be read, no new batch request starts until it can.",
        type="bool",
        default="1",
        flag=True,
    ),
    ConfigEntry(
        "BATCH_MAX_QUEUED_REQUESTS_PER_USER",
        "batch",
        "Unfinished batch requests one user may have across all their batches. A batch "
        "that would go past it is refused, however it is split.",
        type="int",
        default="100000",
        minimum=1,
    ),
    ConfigEntry(
        "BATCH_USER_MAX_CONCURRENCY",
        "batch",
        "Batch requests one user may have running at once across the whole deployment "
        "and every model. 0 removes this cap (the per-role and per-model limits still "
        "apply).",
        type="int",
        default="64",
        minimum=0,
    ),
    *(
        ConfigEntry(
            f"BATCH_ROLE_CONCURRENCY_{role.upper()}",
            "batch",
            f"Batch requests one {role} user may have running at once in each gateway "
            "process. Separate from the role's interactive concurrency limit: batch work "
            "neither waits on nor takes the slots of the user's own interactive requests. "
            "0 removes this cap (the per-user and per-model batch limits still apply).",
            type="int",
            default=str(default),
            minimum=0,
        )
        for role, default in (("free", 8), ("pro", 16), ("internal", 64), ("admin", 64))
    ),
    ConfigEntry(
        "BATCH_INTERACTIVE_RESERVE",
        "batch",
        "Slots of every model's batch cap always left free for interactive requests, on "
        "top of those interactive requests already in flight take (while batch work "
        "yields to interactive traffic). 0 keeps no extra room.",
        type="int",
        default="0",
        minimum=0,
    ),
    ConfigEntry(
        "BATCH_MAX_LINE_KB",
        "batch",
        "Largest line of an uploaded batch input file, in KiB.",
        type="int",
        default="4096",
        minimum=1,
    ),
    ConfigEntry(
        "BATCH_MAX_RESULT_KB",
        "batch",
        "Largest response one batch request may store, in KiB. A larger response fails "
        "the request with result_too_large.",
        type="int",
        default="4096",
        minimum=1,
    ),
    ConfigEntry(
        "BATCH_FILES_MAX_TOTAL_MB_PER_USER",
        "batch",
        "Uploaded batch input files one user may keep at once, in MiB.",
        type="int",
        default="512",
        minimum=1,
    ),
    ConfigEntry(
        "BATCH_FILE_UPLOADS_PER_HOUR",
        "batch",
        "Batch input files one user may upload per hour.",
        type="int",
        default="60",
        minimum=1,
    ),
    ConfigEntry(
        "BATCH_FILE_RETENTION_DAYS",
        "batch",
        "Days an uploaded batch input file is kept before it is deleted.",
        type="int",
        default="30",
        minimum=1,
    ),
    ConfigEntry(
        "BATCH_ANTHROPIC_VERSIONS",
        "batch",
        "Comma-separated anthropic-version values a Message Batch may be created under. "
        "Known versions: 2023-06-01, 2026-10-21. A request with any other version is "
        "refused.",
        default="2023-06-01,2026-10-21",
    ),
    ConfigEntry(
        "BATCH_WEBHOOK_ALLOW_PRIVATE_TARGETS",
        "batch",
        "Let batch webhooks reach private, loopback and link-local addresses. For "
        "isolated test deployments only: on, a user can make the gateway send requests "
        "into its own network. Cloud metadata addresses stay refused.",
        type="bool",
        default="0",
        flag=True,
    ),
    # --- network -----------------------------------------------------------
    _setting(
        "cors_allowed_origins",
        "network",
        "Comma-separated browser origins allowed to call this API with credentials.",
        "list",
    ),
    _setting(
        "trusted_proxies",
        "network",
        "Comma-separated CIDR ranges of the proxies allowed to assert the client "
        "address through X-Forwarded-For or X-Real-IP. Use the narrowest ranges "
        "possible, such as one reverse proxy's /32.",
        "list",
    ),
    _setting(
        "trusted_direct_client_networks",
        "network",
        "Comma-separated private networks whose direct connections are individual "
        "clients rather than shared proxies.",
        "list",
    ),
    _setting(
        "trust_proxy_headers",
        "network",
        "Interpret forwarding headers from TRUSTED_PROXIES at all.",
        "bool",
    ),
    _setting(
        "trusted_cloudflare_networks",
        "network",
        "Comma-separated CIDR ranges of the peers allowed to assert CF-Connecting-IP.",
        "list",
    ),
    _setting(
        "trust_cloudflare_headers",
        "network",
        "Prefer CF-Connecting-IP from TRUSTED_CLOUDFLARE_NETWORKS. Requires TRUST_PROXY_HEADERS.",
        "bool",
    ),
    _setting(
        "trust_x_real_ip",
        "network",
        "Accept X-Real-IP from trusted proxies. Requires TRUST_PROXY_HEADERS.",
        "bool",
    ),
    # --- alerts ------------------------------------------------------------
    _setting(
        "alerts_enabled",
        "alerts",
        "Run the in-process alert engine.",
        "bool",
        restart_required=True,
    ),
    _setting(
        "slack_alerts_webhook_url",
        "alerts",
        "Slack webhook for alerts. Empty uses SLACK_WEBHOOK_URL. "
        "Takes effect after a restart, together with the WeCom channel it alternates with.",
        "str",
        secret=True,
        fallback_key="SLACK_WEBHOOK_URL",
        restart_required=True,
    ),
    _setting(
        "wecom_alerts_webhook_url",
        "alerts",
        "WeCom group-bot webhook for alerts. Empty disables the WeCom channel. "
        "Takes effect after a restart, like the failed-request detector it enables.",
        "str",
        secret=True,
        restart_required=True,
    ),
    _setting(
        "slack_webhook_url",
        "alerts",
        "Slack webhook for the failed-request alerter, and for alerts when "
        "SLACK_ALERTS_WEBHOOK_URL is empty. Empty turns the failed-request alerter off.",
        "str",
        secret=True,
        restart_required=True,
    ),
    _setting(
        "failed_request_alert_threshold",
        "alerts",
        "Failed requests within the window that page.",
        "int",
        restart_required=True,
    ),
    _setting(
        "failed_request_alert_window_minutes",
        "alerts",
        "Window, in minutes, over which failed requests are counted.",
        "int",
        restart_required=True,
    ),
    _setting(
        "failed_request_alert_cooldown_minutes",
        "alerts",
        "Minutes between repeated failed-request pages.",
        "int",
        restart_required=True,
    ),
    _setting(
        "failed_request_alert_rate",
        "alerts",
        "Failing fraction of a window that pages on its own. 0 turns the rate rule off.",
        "float",
        restart_required=True,
    ),
    _setting(
        "failed_request_alert_rate_min_count",
        "alerts",
        "Requests a window needs before the rate rule applies.",
        "int",
        restart_required=True,
    ),
    ConfigEntry(
        "DEPLOYMENT_ENV",
        "alerts",
        "Environment name shown on alerts, such as production or staging. Empty infers "
        "it from BASE_URL.",
    ),
    ConfigEntry(
        "ENVIRONMENT",
        "alerts",
        "Older name of DEPLOYMENT_ENV, used while DEPLOYMENT_ENV is empty.",
    ),
    ConfigEntry(
        "ALERT_HISTORY_ENABLED",
        "alerts",
        "Keep a history of every backend alert, suppression and delivery result in the "
        "database, and accept history from configured sources. Off by default.",
        type="bool",
        default="false",
        restart_required=True,
    ),
    ConfigEntry(
        "ALERT_HISTORY_DEPLOYMENT_ID",
        "alerts",
        "Deployment id written on this gateway's alert history and matched by reader "
        "credentials. Empty is 'default'.",
        restart_required=True,
    ),
    ConfigEntry(
        "ALERT_HISTORY_READ_CREDENTIALS",
        "alerts",
        "JSON list of read-only alert-history credentials: SHA-256 of each token, the "
        "deployments and environments it may read, and its expiry. Tokens themselves are "
        "never stored.",
        type="text",
        secret=True,
    ),
    ConfigEntry(
        "ALERT_HISTORY_WRITER_CREDENTIALS",
        "alerts",
        "JSON list of alert-history source credentials: SHA-256 of each token and the "
        "source, namespace, deployment and environments it may write, and its expiry.",
        type="text",
        secret=True,
    ),
    # --- integrations ------------------------------------------------------
    ConfigEntry(
        "IDENTITY_JWT_PRIVATE_KEY",
        "integrations",
        "Unencrypted RSA private key (PEM, 2048 bits or more) that signs identity tokens "
        "for the cloud agent. Empty means this gateway issues none.",
        type="text",
        secret=True,
    ),
    ConfigEntry(
        "IDENTITY_JWT_RETIRING_PUBLIC_KEYS",
        "integrations",
        "Public keys still published while a key rotation drains, as concatenated PEMs.",
        type="text",
    ),
    ConfigEntry(
        "IDENTITY_ISSUER",
        "integrations",
        "Issuer stamped on identity tokens: this gateway's public origin, no trailing "
        "slash. Empty uses BASE_URL.",
    ),
    ConfigEntry(
        "IDENTITY_ALLOWED_REDIRECTS",
        "integrations",
        "Comma-separated callback URLs an authorization code may be delivered to, matched exactly.",
        type="list",
    ),
    ConfigEntry(
        "GATEWAY_GRANT_DISPATCH_TOKEN",
        "integrations",
        "Shared secret the cloud agent's control plane presents to mint inference grants "
        "and read the internal API. Empty turns the internal API off.",
        secret=True,
    ),
    ConfigEntry(
        "RAG_API_KEY",
        "integrations",
        "API key the documentation assistant uses for its own gateway calls. Empty "
        "disables the assistant.",
        secret=True,
    ),
    ConfigEntry(
        "RAG_API_BASE_URL",
        "integrations",
        "This gateway's own OpenAI-compatible address, as the documentation assistant reaches it.",
        default="http://localhost:8080/v1",
    ),
    ConfigEntry(
        "RAG_EMBEDDER",
        "integrations",
        "How the documentation assistant embeds text: gateway, or the offline hash fallback.",
        default="gateway",
        choices=("gateway", "hash"),
    ),
    ConfigEntry("RAG_EMBED_MODEL", "integrations", "Embedding model.", default="bge-m3"),
    ConfigEntry(
        "RAG_CHAT_MODEL",
        "integrations",
        "Model that writes the documentation assistant's answers.",
        default="qwen3.6-35b",
    ),
    ConfigEntry(
        "RAG_TOP_K",
        "integrations",
        "Passages retrieved per question.",
        type="int",
        default="4",
        minimum=1,
    ),
    ConfigEntry(
        "RAG_MAX_TOKENS",
        "integrations",
        "Output budget of an answer.",
        type="int",
        default="1024",
        minimum=1,
    ),
    ConfigEntry(
        "RAG_TEMPERATURE",
        "integrations",
        "Sampling temperature of an answer.",
        type="float",
        default="0.3",
        minimum=0,
    ),
    ConfigEntry(
        "RAG_INDEX_PATH",
        "integrations",
        "Prebuilt documentation index. Empty uses the distribution overlay's.",
    ),
    ConfigEntry(
        "RAG_CORPUS_DIR",
        "integrations",
        "Documentation source the ingest tool reads. Empty uses the distribution overlay's.",
    ),
    ConfigEntry(
        "RAG_CHUNK_MAX_CHARS",
        "integrations",
        "Largest chunk the ingest tool cuts, in characters.",
        type="int",
        default="1200",
        minimum=1,
    ),
    ConfigEntry(
        "RAG_CHUNK_OVERLAP_CHARS",
        "integrations",
        "Characters shared by neighboring chunks.",
        type="int",
        default="150",
        minimum=0,
    ),
    ConfigEntry(
        "RAG_GATEWAY_BASE_URL",
        "integrations",
        "Gateway the ingest tool embeds through.",
        default="http://localhost:8080/v1",
    ),
    ConfigEntry(
        "RAG_GATEWAY_API_KEY",
        "integrations",
        "API key the ingest tool embeds with. Empty uses LOCAL_API_KEY.",
        secret=True,
        fallback_key="LOCAL_API_KEY",
    ),
    _setting("qdrant_base_url", "integrations", "Qdrant vector database URL.", "str"),
    _setting("qdrant_api_key", "integrations", "Qdrant API key.", "str", secret=True),
    # --- privacy -----------------------------------------------------------
    _setting(
        "db_store_full_content",
        "privacy",
        "Store prompts and responses verbatim in the request log. Off, they are never "
        "written. Weigh your users' expectations before turning it on.",
        "bool",
        restart_required=True,
    ),
    _setting(
        "erasure_fence_secret",
        "privacy",
        "Key for the records that keep deleted accounts deleted. Empty uses "
        "API_KEY_SECRET. It cannot be changed once the gateway has started with either: "
        "the deletion records are tied to it.",
        "str",
        secret=True,
        restart_required=True,
        immutable=True,
        fallback_key="API_KEY_SECRET",
    ),
    _setting(
        "erasure_fence_protocol_ready",
        "privacy",
        "Allow permanent account deletion. Leave it off until every process that writes "
        "the request log checks the erasure fence.",
        "bool",
    ),
)

#: Names that stay in the environment: needed before the database is reachable,
#: or describing the container and network rather than the application.
ENVIRONMENT_ONLY: Mapping[str, str] = {
    "DB_HOST": "database connection",
    "DB_PORT": "database connection",
    "DB_NAME": "database connection",
    "DB_USER": "database connection",
    "DB_PASSWORD": "database connection",
    "DB_ENABLED": "database connection",
    "MODELS_CONFIG_PATH": "configuration file location",
    "MODELS_CONFIG": "configuration file location",
    "ROUTING_CONFIG_PATH": "configuration file location",
    "ROUTING_CONFIG": "configuration file location",
    "ALERTS_CONFIG_PATH": "configuration file location",
    "DISTRIBUTION_CONFIG_PATH": "configuration file location",
    "DISTRIBUTION_CONFIG_MODE": "configuration file location",
    "LOG_FILE": "process wiring",
    "BACKEND_EXTENSIONS": "process wiring: imports code at boot",
    "GEOIP_COUNTRY_DB": "process wiring",
    "GEOIP_COUNTRY_PROVIDER": "process wiring",
    "WEB_CONCURRENCY": "process wiring",
    "UVICORN_WORKERS": "process wiring",
    "GUNICORN_WORKERS": "process wiring",
    "SUPERVISOR_RESTARTS_BACKEND": "process wiring: declares the supervisor's restart policy",
    "REFRESH_TOKEN_COOKIE_NAME": "process wiring: the console container reads the same name",
}

#: ``Settings`` fields deliberately left out of the registry, and why.
UNREGISTERED_SETTINGS_FIELDS: Mapping[str, str] = {
    "turnstile_site_key": "The console renders the captcha with its own site key; the "
    "backend never reads this one.",
    "trusted_proxies_parsed": "Derived from TRUSTED_PROXIES.",
    "trusted_direct_client_parsed": "Derived from TRUSTED_DIRECT_CLIENT_NETWORKS.",
    "trusted_cloudflare_parsed": "Derived from TRUSTED_CLOUDFLARE_NETWORKS.",
}

#: Settings fields a validator derives from registered ones. The overlay copies
#: them along with the fields they are derived from.
DERIVED_SETTINGS_FIELDS: tuple[str, ...] = (
    "trusted_proxies_parsed",
    "trusted_direct_client_parsed",
    "trusted_cloudflare_parsed",
)

#: Environment-variable spellings of the runtime settings, which ``site_settings``
#: holds and the Settings tab edits.
RUNTIME_SETTING_NAMES: frozenset[str] = frozenset(key.upper() for key in RUNTIME_SETTINGS_REGISTRY)

CUSTOM_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")

#: Highest numbered suffix the provider key pools scan (``dynamic_keys``).
MAX_NUMBERED_KEY = 20

#: A name inside a ``${VAR}`` or ``${VAR:-default}`` reference.
_REFERENCE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
#: A whole-value ``${VAR}``, the only form the model registry expands.
_WHOLE_REFERENCE_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
#: Words that mark a name as a credential's (:func:`is_secret_name`), matched
#: anywhere in the upper-cased name. The console's add-variable form mirrors
#: this list; keep the two in step.
SECRET_NAME_WORDS: tuple[str, ...] = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASS",
    "PWD",
    "WEBHOOK",
    "PRIVATE",
    "AUTH",
    "CREDENTIAL",
    "BEARER",
    "CERT",
    "DSN",
    "SA_JSON",
)


def _provider_key_vars() -> dict[str, tuple[str, str]]:
    # Imported lazily: the adapters package imports this configuration.
    from serving.adapters.dynamic_keys import _PROVIDER_ENV_KEY_VARS

    return dict(_PROVIDER_ENV_KEY_VARS)


_EXTENSION_ENTRIES: dict[str, ConfigEntry] = {}


def invalidate_config_registry() -> None:
    """Refresh cached metadata after startup extension/provider registration."""
    static_entries.cache_clear()
    _static_by_key.cache_clear()


def register_config_entries(entries: Iterable[ConfigEntry]) -> None:
    """Register an extension's own settings before application configuration loads.

    Extensions use ``field=None`` and read through ``config_value``. Their
    settings receive the same validation, persistence, secret masking and live
    refresh as built-ins. Registration is atomic and cannot replace existing
    entries. Extensions must use existing categories.
    """
    additions: dict[str, ConfigEntry] = {}
    existing = {entry.key for entry in static_entries()}
    for entry in entries:
        if not CUSTOM_KEY_RE.fullmatch(entry.key):
            raise ValueError(f"Invalid extension configuration key: {entry.key!r}")
        if entry.key in existing or entry.key in additions:
            raise ValueError(f"Configuration key already registered: {entry.key}")
        if is_environment_only(entry.key) or is_runtime_setting_name(entry.key):
            raise ValueError(f"Configuration key is reserved: {entry.key}")
        if entry.field is not None or entry.origin != "static":
            raise ValueError("Extension settings must have field=None and origin=static")
        if entry.category not in CATEGORY_IDS or not entry.description.strip():
            raise ValueError(f"Invalid extension configuration metadata: {entry.key}")
        additions[entry.key] = entry
    _EXTENSION_ENTRIES.update(additions)
    invalidate_config_registry()


def extension_entry(key: str) -> ConfigEntry | None:
    """Read an extension declaration without importing providers or configuration."""
    return _EXTENSION_ENTRIES.get(key)


@lru_cache(maxsize=1)
def static_entries() -> tuple[ConfigEntry, ...]:
    """Return built-in and explicitly registered extension configuration."""
    from serving.utils.delivery import email_provider_names, sms_provider_names

    choices = {
        "EMAIL_PROVIDER": email_provider_names(),
        "SMS_PROVIDER": sms_provider_names(),
    }
    base_entries = tuple(
        replace(entry, choices=choices[entry.key]) if entry.key in choices else entry
        for entry in _STATIC_ENTRIES
    )
    by_key = {entry.key: entry for entry in (*base_entries, *_EXTENSION_ENTRIES.values())}
    provider_keys: list[ConfigEntry] = []
    for provider, (base_var, _numbered_prefix) in sorted(_provider_key_vars().items()):
        if base_var in by_key:
            continue
        provider_keys.append(
            ConfigEntry(
                base_var,
                "providers",
                f"API key for {provider}. Numbered extras ({base_var}2, {base_var}3, ...) "
                "join the same key pool.",
                secret=True,
                restart_required=True,
            )
        )
    # Provider keys sit before the routing entries, after the other providers.
    index = next(i for i, entry in enumerate(base_entries) if entry.category == "routing")
    return (
        *base_entries[:index],
        *provider_keys,
        *base_entries[index:],
        *_EXTENSION_ENTRIES.values(),
    )


def static_entry(key: str) -> ConfigEntry | None:
    """Return the static entry for *key*, if there is one."""
    return _static_by_key().get(key)


@lru_cache(maxsize=1)
def _static_by_key() -> dict[str, ConfigEntry]:
    return {entry.key: entry for entry in static_entries()}


def is_offered(entry: ConfigEntry) -> bool:
    """Return whether the admin console offers *entry* for editing.

    It offers neither a setting with a single accepted value, which leaves
    nothing to choose, nor the settings of a category that cannot take effect
    here: the phone settings do nothing until an extension registers an SMS
    provider.
    """
    if len(entry.choices) == 1:
        return False
    category = _CATEGORY_BY_ID.get(entry.category)
    return category is None or category.in_effect is None or category.in_effect()


def is_environment_only(key: str) -> bool:
    """Return whether *key* stays in the environment."""
    return key in ENVIRONMENT_ONLY


def is_runtime_setting_name(key: str) -> bool:
    """Return whether *key* is the environment spelling of a runtime setting."""
    return key in RUNTIME_SETTING_NAMES


def numbered_key_entry(key: str) -> ConfigEntry | None:
    """Return the entry for a numbered provider key such as ``MINIMAX_API_KEY2``."""
    for provider, (base_var, numbered_prefix) in _provider_key_vars().items():
        if not key.startswith(numbered_prefix):
            continue
        suffix = key[len(numbered_prefix) :]
        if not suffix.isdigit() or suffix.startswith("0"):
            continue
        if not 1 <= int(suffix) <= MAX_NUMBERED_KEY:
            continue
        return ConfigEntry(
            key,
            "providers",
            f"Additional API key for {provider}, pooled with {base_var}.",
            secret=True,
            restart_required=True,
            origin="numbered",
        )
    return None


def custom_entry(key: str, *, secret: bool = False) -> ConfigEntry:
    """Return the entry for an administrator-added variable.

    It is secret when *secret* says so or when its name looks like a
    credential's (:func:`is_secret_name`); a flag can add secrecy, never take
    the name's away.
    """
    return ConfigEntry(
        key,
        "providers",
        "Added by an administrator for a reference in the model registry.",
        secret=secret or is_secret_name(key),
        restart_required=True,
        origin="custom",
    )


def is_secret_name(name: str) -> bool:
    """Return whether *name* looks like a credential's: a variable's, or a field's.

    True when the upper-cased name contains any of :data:`SECRET_NAME_WORDS`.
    It decides the secrecy of discovered and custom variables, and which
    configuration-file fields carry credentials (``api_key``, ``token``,
    ``Authorization``).
    """
    upper = name.upper()
    return any(word in upper for word in SECRET_NAME_WORDS)


@dataclass
class DiscoveredReference:
    """A ``${VAR}`` reference found in the active configuration files."""

    key: str
    category: str
    used_by: set[str]
    #: A non-optional model route fails without it.
    required: bool = False
    #: ``api_keys`` lists the variable belongs to; such a list fails only when
    #: every member is empty, so its members are required together.
    key_groups: list[tuple[str, ...]] | None = None
    #: Referenced from a field that carries a credential (``api_key``,
    #: ``api_keys``, any field whose name looks like a credential's), so it is
    #: secret whatever its own name.
    credential: bool = False


def _references(value: Any) -> set[str]:
    """Return every variable name referenced anywhere inside *value*."""
    names: set[str] = set()
    if isinstance(value, str):
        names.update(_REFERENCE_RE.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            names.update(_references(item))
    elif isinstance(value, list):
        for item in value:
            names.update(_references(item))
    return names


def _credential_references(value: Any) -> set[str]:
    """Return every variable referenced under a field that carries a credential.

    Such a field is named like a credential (:func:`is_secret_name`): ``api_key``
    and ``api_keys`` in the model registry, ``token``, ``password``, an
    ``Authorization`` header. Whatever a variable is called, the value it lends
    that field is a secret.
    """
    names: set[str] = set()
    if isinstance(value, dict):
        for field_name, item in value.items():
            if is_secret_name(str(field_name)):
                names.update(_references(item))
            else:
                names.update(_credential_references(item))
    elif isinstance(value, list):
        for item in value:
            names.update(_credential_references(item))
    return names


def _whole_reference(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _WHOLE_REFERENCE_RE.fullmatch(value.strip())
    return match.group(1) if match else None


def _load_yaml(path: Path | None) -> Any:
    if path is None or not path.is_file():
        return None
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None


def _note(
    found: dict[str, DiscoveredReference],
    key: str,
    category: str,
    used_by: Iterable[str] = (),
) -> DiscoveredReference:
    reference = found.get(key)
    if reference is None:
        reference = found[key] = DiscoveredReference(key, category, set())
    reference.used_by.update(model_id for model_id in used_by if model_id)
    return reference


def _discover_models(data: Any, found: dict[str, DiscoveredReference]) -> None:
    """Record the model registry's references and which of them routes need.

    A route needs exactly what makes the registry skip its model when empty — a
    whole-value ``${VAR}`` in ``base_url``, ``api_key`` or ``embeddings_path``
    (the model's own ``base_url``/``api_key`` when the route has none), and at
    least one member of ``api_keys``. Routes marked ``optional: true`` are skipped
    rather than failing their model, so they need nothing.
    """
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or "")
        for key in _references(model):
            _note(found, key, "providers", [model_id])
        routes = model.get("route") or [
            {"base_url": model.get("base_url"), "api_key": model.get("api_key")}
        ]
        if not isinstance(routes, list):
            continue
        for route in routes:
            if not isinstance(route, dict) or route.get("optional"):
                continue
            needed = [
                _whole_reference(route.get("base_url") or model.get("base_url")),
                _whole_reference(route.get("embeddings_path")),
            ]
            api_keys = route.get("api_keys")
            if isinstance(api_keys, list):
                group = tuple(key for key in (_whole_reference(item) for item in api_keys) if key)
                # A literal key in the list keeps the route alive on its own.
                if group and len(group) == len(api_keys):
                    for key in group:
                        reference = _note(found, key, "providers", [model_id])
                        reference.key_groups = [*(reference.key_groups or []), group]
            else:
                needed.append(_whole_reference(route.get("api_key") or model.get("api_key")))
            for key in needed:
                if key:
                    _note(found, key, "providers", [model_id]).required = True


def _discover_routing(data: Any, found: dict[str, DiscoveredReference]) -> None:
    if not isinstance(data, dict):
        return
    for section in ("local_deployment", "remote_deployment"):
        deployments = data.get(section)
        if not isinstance(deployments, list):
            continue
        for deployment in deployments:
            models = deployment.get("models") if isinstance(deployment, dict) else None
            model_ids = [str(model) for model in models] if isinstance(models, list) else []
            for key in _references(deployment):
                _note(found, key, "providers", model_ids)
    for key in _references(data):
        _note(found, key, "providers")


def _discover_alerts(data: Any, found: dict[str, DiscoveredReference]) -> None:
    for key in _references(data):
        _note(found, key, "alerts")


def discover_references(
    models_path: Path | None,
    routing_path: Path | None,
    alerts_path: Path | None,
) -> dict[str, DiscoveredReference]:
    """Find every ``${VAR}`` reference in the three configuration files.

    Files are parsed rather than searched, so a ``${VAR}`` in a comment counts
    for nothing. A missing or unreadable file contributes no references. A
    reference from a field that carries a credential is marked as one
    (:attr:`DiscoveredReference.credential`).
    """
    found: dict[str, DiscoveredReference] = {}
    models, routing, alerts = (
        _load_yaml(path) for path in (models_path, routing_path, alerts_path)
    )
    _discover_models(models, found)
    _discover_routing(routing, found)
    _discover_alerts(alerts, found)
    for data in (models, routing, alerts):
        for key in _credential_references(data):
            if key in found:
                found[key].credential = True
    return found


def _group_required(groups: list[tuple[str, ...]]) -> Callable[[RequirementContext], bool]:
    def required(context: RequirementContext) -> bool:
        return any(not any(context.value(key).strip() for key in group) for group in groups)

    return required


def _merge_requirements(first: Requirement, second: Requirement) -> Requirement:
    if first is True or second is True:
        return True
    if first is False:
        return second
    if second is False:
        return first

    def either(context: RequirementContext) -> bool:
        return bool(first(context) or second(context))  # type: ignore[operator]

    return either


def _discovered_requirement(reference: DiscoveredReference) -> Requirement:
    requirement: Requirement = reference.required
    if reference.key_groups:
        requirement = _merge_requirements(requirement, _group_required(reference.key_groups))
    return requirement


def build_entries(
    discovered: Mapping[str, DiscoveredReference],
    stored: Mapping[str, bool],
    environ: Mapping[str, str],
    reset: Mapping[str, bool] | None = None,
) -> dict[str, ConfigEntry]:
    """Assemble every entry, in the order the console lists them.

    A row's secret flag is one-way: an entry whose row (a value or a reset
    marker) is marked secret is secret, whatever the registry, the reference
    or the name would make it.

    Args:
        discovered: References found by :func:`discover_references`.
        stored: Keys that have a database row, mapped to the row's secret flag.
        environ: The process environment, for numbered provider keys.
        reset: Keys an administrator reset, mapped to the reset row's secret
            flag. They keep it, but a reset row lists no entry of its own.

    Returns:
        Entries keyed by name: static, then discovered, numbered and custom.
    """
    entries: dict[str, ConfigEntry] = {entry.key: entry for entry in static_entries()}

    for key, reference in discovered.items():
        if is_environment_only(key) or is_runtime_setting_name(key):
            continue
        requirement = _discovered_requirement(reference)
        used_by = tuple(sorted(reference.used_by))
        existing = entries.get(key)
        if existing is not None:
            entries[key] = replace(
                existing,
                secret=existing.secret or reference.credential,
                required=_merge_requirements(existing.required, requirement),
                restart_required=True,
                used_by=used_by,
            )
            continue
        numbered = numbered_key_entry(key)
        if numbered is not None:
            entries[key] = replace(numbered, required=requirement, used_by=used_by)
            continue
        entries[key] = ConfigEntry(
            key,
            reference.category,
            "Referenced by the model registry."
            if reference.category == "providers"
            else "Referenced by the alert rules.",
            secret=reference.credential or is_secret_name(key),
            required=requirement,
            restart_required=True,
            origin="discovered",
            used_by=used_by,
        )

    present = {key for key, value in environ.items() if value} | set(stored)
    for key in sorted(present):
        if key in entries:
            continue
        numbered = numbered_key_entry(key)
        if numbered is not None:
            entries[key] = numbered

    for key in sorted(stored):
        if key in entries or is_environment_only(key) or is_runtime_setting_name(key):
            continue
        entries[key] = custom_entry(key)

    for flags in (stored, reset or {}):
        for key, secret in flags.items():
            entry = entries.get(key)
            if secret and entry is not None and not entry.secret:
                entries[key] = replace(entry, secret=True)
    return entries
