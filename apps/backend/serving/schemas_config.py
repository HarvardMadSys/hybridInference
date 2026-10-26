"""Pydantic schemas for the database-backed configuration admin API."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — pydantic resolves the annotation at runtime
from typing import Any, Literal

from pydantic import BaseModel, Field

#: A value in its typed form; a ``list`` travels as its comma-separated string.
ConfigScalar = bool | int | float | str


class ConfigCategory(BaseModel):
    """A group of settings in the Configuration tab."""

    id: str
    label: str
    description: str


class ConfigEntryItem(BaseModel):
    """One setting as the admin console shows it. Secrets carry no value."""

    key: str = Field(..., description="Canonical environment-variable name")
    category: str
    description: str
    type: Literal["str", "text", "int", "float", "bool", "list"]
    secret: bool = Field(..., description="Write-only: value and default are always null")
    required: bool
    missing: bool = Field(..., description="Required and empty")
    is_set: bool
    value: ConfigScalar | None = Field(
        None,
        description="Value in force, typed; null for secrets. A stored value that cannot apply "
        "shows the environment value or default that applies instead",
    )
    default: ConfigScalar | None = Field(None, description="Built-in default; null for secrets")
    source: Literal["database", "environment", "default"] = Field(
        ..., description="Where the value in force comes from"
    )
    restart_required: bool = Field(..., description="Captured at startup")
    pending_restart: bool = Field(..., description="Changed since the process booted")
    environment_ignored: bool = Field(
        ...,
        description="The stored value is in force, and the environment has a different one it "
        "overrides",
    )
    immutable: bool = Field(..., description="Cannot be changed once set")
    locked: bool = Field(
        ...,
        description="Immutable and already settled, by its own value or the one in its place: "
        "no value can be stored now",
    )
    setup: bool = Field(..., description="Shown on the first-run configuration step")
    origin: Literal["static", "discovered", "numbered", "custom"] = Field(
        ...,
        description="Where the entry comes from: built in (static), a ${VAR} reference in a "
        "configuration file (discovered), a numbered provider key, or added by an "
        "administrator (custom). Only a static entry ignores the secret flag",
    )
    custom: bool = Field(..., description="Added by an administrator (origin is custom)")
    invalid: str | None = Field(
        None, description="Why the stored value could not be applied; null without one"
    )
    used_by: list[str] = Field(default_factory=list, description="Model ids that reference it")
    updated_at: datetime | None = None
    updated_by: str | None = None


class ConfigResponse(BaseModel):
    """Every setting, grouped by category."""

    categories: list[ConfigCategory]
    entries: list[ConfigEntryItem]
    missing: list[str] = Field(..., description="Keys of the missing entries")
    pending_restart: list[str] = Field(..., description="Keys of the pending-restart entries")
    restart_supported: bool = Field(
        ..., description="Whether POST /admin/system/restart can bring the process back"
    )


class UpdateConfigRequest(BaseModel):
    """A batch of values and secret flags to store; validated and written together."""

    values: dict[str, ConfigScalar | None] = Field(
        default_factory=dict,
        description="A JSON boolean for bool, a number for int/float, a string otherwise",
    )
    secrets: dict[str, bool] = Field(
        default_factory=dict,
        description="Keys to mark secret; only true counts, and a secret is never made public "
        "again. For custom, discovered and numbered keys, whether this request writes them or "
        "they already have a stored row (a key absent from values keeps its value); built-in "
        "settings keep their own flag. A custom key named like a credential is secret anyway",
    )


class RestartResponse(BaseModel):
    """Acknowledgement that the backend is restarting."""

    restarting: bool
    instance: str = Field(
        ...,
        description="The process that is exiting; /health reports another once it is back",
    )


class ConfigBackupAppConfigRow(BaseModel):
    """One stored ``app_config`` row in a configuration backup."""

    key: str
    value: str = ""
    secret: bool = False
    source: Literal["admin", "setup", "env_import", "generated", "reset"] = "admin"


class ConfigBackupSiteSetting(BaseModel):
    """One admin-editable ``site_settings`` row in a configuration backup."""

    key: str
    value: str
    value_type: str = "str"


class ConfigBackupProviderDefinition(BaseModel):
    """One custom provider definition in a configuration backup."""

    provider: str
    display_name: str
    adapter_kind: str = "openai_compat"
    default_base_url: str
    status: Literal["active", "disabled"] = "active"


class ConfigBackupProviderKey(BaseModel):
    """One dynamic provider API key (with plaintext secret) in a configuration backup."""

    id: str
    provider: str
    api_key: str
    label: str | None = None
    status: Literal["active", "disabled"] = "active"
    min_role: str = "free"


class ConfigBackupDisabledEnvKey(BaseModel):
    """One disabled environment provider API key tombstone in a configuration backup."""

    provider: str
    key_hash: str
    key_prefix: str


class ConfigBackupEnvKeyMinRole(BaseModel):
    """One environment provider API key tier reservation in a configuration backup."""

    provider: str
    key_hash: str
    key_prefix: str
    min_role: str


class ConfigBackupRouteState(BaseModel):
    """One provider route state (disabled and/or removed) in a configuration backup."""

    model_id: str
    route_id: str
    disabled: bool = False
    removed: bool = False


class ConfigBackupRouteCandidate(BaseModel):
    """One runtime provider route candidate in a configuration backup."""

    model_id: str
    route_id: str
    route_type: str
    provider: str
    openrouter_sort: str | None = None
    deployment_type: str = "auto"
    base_url: str
    api_key_id: str | None = None
    provider_model_id: str
    quota_limit: int | None = None
    concurrency_limit: int | None = None
    weight: float = 1.0
    pricing: dict[str, str] | None = None
    capabilities: dict[str, Any] | None = None
    endpoint_id: str | None = None


class ConfigBackupRouteConfig(BaseModel):
    """One config-route target override in a configuration backup."""

    model_id: str
    route_id: str
    provider: str
    openrouter_sort: str | None = None
    deployment_type: str | None = None
    base_url: str
    api_key_id: str | None = None
    provider_model_id: str
    quota_limit: int | None = None
    concurrency_limit: int | None = None


class ConfigBackupRouteCapability(BaseModel):
    """One route capability override in a configuration backup."""

    model_id: str
    route_id: str
    capabilities: dict[str, Any]


class ConfigBackupWeightOverride(BaseModel):
    """One endpoint weight override in a configuration backup."""

    model_id: str
    endpoint_id: str
    weight: float


class ConfigBackupModelVisibility(BaseModel):
    """One model visibility override in a configuration backup."""

    model_id: str
    required_role: str


class ConfigBackupSignupDomain(BaseModel):
    """One allowed signup email domain in a configuration backup."""

    domain: str
    is_wildcard: bool = False


class ConfigBackupPayload(BaseModel):
    """Complete backup bundle of all administrator-configurable state."""

    schema_version: int = Field(1, description="Backup schema version")
    exported_at: datetime | None = Field(None, description="UTC timestamp when the backup was made")
    app_config: list[ConfigBackupAppConfigRow] = Field(default_factory=list)
    site_settings: list[ConfigBackupSiteSetting] = Field(default_factory=list)
    provider_definitions: list[ConfigBackupProviderDefinition] = Field(default_factory=list)
    provider_keys: list[ConfigBackupProviderKey] = Field(default_factory=list)
    disabled_provider_env_keys: list[ConfigBackupDisabledEnvKey] = Field(default_factory=list)
    provider_env_key_min_roles: list[ConfigBackupEnvKeyMinRole] = Field(default_factory=list)
    disabled_providers: list[str] = Field(default_factory=list)
    provider_route_states: list[ConfigBackupRouteState] = Field(default_factory=list)
    provider_route_candidates: list[ConfigBackupRouteCandidate] = Field(default_factory=list)
    provider_route_configs: list[ConfigBackupRouteConfig] = Field(default_factory=list)
    provider_route_capabilities: list[ConfigBackupRouteCapability] = Field(default_factory=list)
    provider_weight_overrides: list[ConfigBackupWeightOverride] = Field(default_factory=list)
    model_visibility_overrides: list[ConfigBackupModelVisibility] = Field(default_factory=list)
    model_concurrency_exemptions: list[str] = Field(default_factory=list)
    batch_model_limits: dict[str, int] = Field(default_factory=dict)
    signup_allowed_domains: list[ConfigBackupSignupDomain] = Field(default_factory=list)


class ConfigRestoreResponse(BaseModel):
    """Result of restoring a configuration backup."""

    config: ConfigResponse
    immutable_keys_preserved: list[str] = Field(
        default_factory=list,
        description="Immutable keys whose existing target values were kept",
    )
