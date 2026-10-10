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
    value: ConfigScalar | None = Field(None, description="Effective value, typed; null for secrets")
    default: ConfigScalar | None = Field(None, description="Built-in default; null for secrets")
    source: Literal["database", "environment", "default"]
    restart_required: bool = Field(..., description="Captured at startup")
    pending_restart: bool = Field(..., description="Changed since the process booted")
    environment_ignored: bool = Field(
        ..., description="The environment has a different value that the database overrides"
    )
    immutable: bool
    setup: bool = Field(..., description="Shown on the first-run configuration step")
    custom: bool = Field(..., description="Added by an administrator")
    invalid: str | None = Field(None, description="Why the stored value could not be applied")
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
    """A batch of values to store; validated and written together."""

    values: dict[str, ConfigScalar | None] = Field(
        ...,
        description="A JSON boolean for bool, a number for int/float, a string otherwise",
    )
    secrets: dict[str, bool] = Field(
        default_factory=dict,
        description="The secret flag for custom variables this request adds",
    )


class RestartResponse(BaseModel):
    """Acknowledgement that the backend is restarting."""

    restarting: bool
