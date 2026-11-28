"""Validated catalog of MCP servers the playground offers as a store.

The document holds discovery metadata and a connection target. Credentials
stay in separate secret settings: a header value is a whole ``${NAME}``
reference, resolved only when the gateway opens the connection.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HEADER_REFERENCE = re.compile(r"^\$\{MCP_[A-Za-z0-9_]+\}$")
_MAX_HEADERS = 32


def _http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("must be an http or https URL")
    if parsed.username or parsed.password:
        raise ValueError("must not include credentials")
    return value


class McpServerEntry(BaseModel):
    """One server the playground store can offer."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    url: str = Field(min_length=1, max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict)
    docs_url: str = Field(default="", max_length=2048)
    icon_url: str = Field(default="", max_length=2048)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("must use lowercase letters, digits, _ or -, starting alphanumeric")
        return value

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value: str) -> str:
        return _http_url(value)

    @field_validator("docs_url", "icon_url")
    @classmethod
    def _valid_optional_url(cls, value: str) -> str:
        return value if not value else _http_url(value)

    @field_validator("headers")
    @classmethod
    def _bounded_headers(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > _MAX_HEADERS:
            raise ValueError(f"must hold at most {_MAX_HEADERS} headers")
        for name, reference in value.items():
            if not _HEADER_REFERENCE.fullmatch(reference):
                raise ValueError(
                    f"header {name} must reference an MCP_ secret setting as ${{MCP_NAME}}"
                )
        return value


class McpServersDocument(BaseModel):
    """The whole ``MCP_SERVERS`` document."""

    model_config = ConfigDict(extra="forbid")

    servers: list[McpServerEntry] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _unique_ids(self) -> McpServersDocument:
        seen: set[str] = set()
        for server in self.servers:
            if server.id in seen:
                raise ValueError(f"duplicate server id: {server.id}")
            seen.add(server.id)
        return self
