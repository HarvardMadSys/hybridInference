"""Pydantic schemas for the database-backed configuration admin API."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — pydantic resolves the annotation at runtime
from typing import Literal

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
