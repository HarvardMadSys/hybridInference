"""The deployment's MCP server registry — one YAML file, no code per server.

MCP's value to this platform is the ecosystem, not the protocol: GitHub,
Sentry, Notion and the rest already publish servers, and a registry of them
exists upstream. So the goal here is that adopting one costs a stanza of
configuration and nothing else. This module is that stanza's schema, and it is
deliberately shaped like ``models.yaml``: a registry of remote services, with
``${VAR}`` interpolation for the credentials, owned by the deployment overlay
rather than by this repository.

Only **remote** servers are described here — a URL the gateway can reach over
HTTPS, spoken to with MCP's streamable-HTTP transport. That is not a temporary
limitation to be filled in later with ``command:``; it follows from where the
sandbox sits. A stdio server is third-party *code*, and there are only two
places to run it: inside the sandbox, where the agent phase has no egress and a
server that wraps an external API is therefore useless, or inside the gateway,
which is the trusted side of the whole design. Neither is acceptable, so
neither is offered. Hosting stdio servers needs its own isolated tier, and that
is a separate piece of work.

Two properties are load-bearing:

- **A credential named by an unset variable disables its server.** The
  alternative — expanding ``${GITHUB_MCP_TOKEN}`` to the empty string, as the
  routing loader does for model endpoints — would send ``Authorization: Bearer``
  upstream and turn a missing secret into a puzzling 401 in the middle of
  somebody's job. Here it is a startup-time refusal naming the variable.
- **Credentials live in this registry and nowhere else.** They are attached by
  the proxy, on the gateway. Nothing in this file ever reaches the sandbox: the
  agent is given a gateway URL and its own job token, and learns neither the
  upstream address nor its key.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from serving.utils.logging import get_logger

logger = get_logger(__name__)

# A server name is not decoration: it is interpolated into the proxy's URL path
# and used as the server's key in the agent CLI's own MCP config, where it also
# prefixes every tool name the model sees (``mcp__github__get_issue``). Keeping
# it to this charset means it can never introduce a path segment, a query, or a
# tool-name separator.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")

# ``${VAR}`` only — no ``:-default`` form, unlike routing.yaml. A default would
# mean a credential silently falling back to something, which is the failure
# this loader exists to prevent.
_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")

# Tool names as they appear in an MCP ``tools/list`` result. Same reasoning as
# the server name: it is compared against, logged, and shown in the UI.
_TOOL_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# Header names the registry refuses to let a deployment set, because the proxy
# owns them and a config that overrode them would break the hop rather than
# customize it.
_RESERVED_HEADERS = frozenset(
    {"host", "content-length", "content-type", "accept", "connection", "transfer-encoding"}
)


class McpRegistryError(Exception):
    """Raised when the MCP registry is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class McpServer:
    """One remote MCP server this deployment offers to agent jobs."""

    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    # An allowlist of tool names. Empty means "whatever the server exposes",
    # which is honest but unbounded: see ``unfiltered``.
    tools: frozenset[str] = frozenset()
    description: str = ""
    # Offered to a job that does not name its servers explicitly. A deployment
    # dogfooding one repository wants its issue tracker on by default; a
    # multi-tenant one probably wants nothing on by default.
    default: bool = False

    @property
    def unfiltered(self) -> bool:
        """Whether every tool this server exposes is reachable."""
        return not self.tools

    def allows(self, tool: str) -> bool:
        """Whether ``tool`` may be listed and called through this server."""
        return self.unfiltered or tool in self.tools

    def public(self) -> dict[str, Any]:
        """Render what a job owner may see: never the URL or the headers.

        The upstream address and its credential are the two things this whole
        indirection exists to keep on the gateway, so they are absent by
        construction rather than filtered at each call site.
        """
        return {
            "name": self.name,
            "description": self.description,
            "tools": sorted(self.tools),
            "default": self.default,
        }


@dataclass(frozen=True)
class McpRegistry:
    """Every server this deployment has configured, by name."""

    servers: dict[str, McpServer] = field(default_factory=dict)

    def get(self, name: str) -> McpServer | None:
        """Return a configured server, or ``None``."""
        return self.servers.get(name)

    @property
    def names(self) -> list[str]:
        """Configured server names, sorted."""
        return sorted(self.servers)

    @property
    def defaults(self) -> list[str]:
        """Servers a job gets when it does not choose for itself."""
        return sorted(name for name, server in self.servers.items() if server.default)

    def resolve(self, requested: list[str] | None) -> list[str]:
        """Turn a job's request into the server list it will actually run with.

        ``None`` means the job did not choose and takes the deployment's
        defaults; an explicit ``[]`` means it wants none. Unknown names raise —
        a job that asked for a tool surface it will not get should fail at
        creation, not discover mid-run that the agent is missing the one
        capability the task depended on.
        """
        if requested is None:
            return self.defaults
        unknown = sorted({name for name in requested if name not in self.servers})
        if unknown:
            known = ", ".join(self.names) or "none configured"
            raise McpRegistryError(
                f"unknown MCP server(s): {', '.join(unknown)}. This deployment offers: {known}"
            )
        return sorted(set(requested))

    def public(self) -> list[dict[str, Any]]:
        """Render the whole registry for the task composer."""
        return [self.servers[name].public() for name in self.names]


def _expand(value: str, *, where: str) -> str:
    """Interpolate ``${VAR}``, refusing to paper over an unset variable."""

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        resolved = os.getenv(key)
        if not resolved:
            raise McpRegistryError(
                f"{where} references ${{{key}}}, which is unset or empty. Set it, or "
                "remove the server — an MCP server configured with a blank credential "
                "would fail every call from inside a job, where the error is hardest to read."
            )
        return resolved

    return _ENV_PATTERN.sub(repl, value)


def _parse_headers(raw: Any, *, name: str) -> dict[str, str]:
    """Validate and interpolate one server's request headers."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise McpRegistryError(f"MCP server {name!r}: headers must be a mapping")
    headers: dict[str, str] = {}
    for key, value in raw.items():
        header = str(key).strip()
        if header.lower() in _RESERVED_HEADERS:
            raise McpRegistryError(
                f"MCP server {name!r}: header {header!r} is set by the proxy and "
                "cannot be configured"
            )
        headers[header] = _expand(str(value), where=f"MCP server {name!r} header {header!r}")
    return headers


def _parse_url(raw: Any, *, name: str) -> str:
    """Validate one server's endpoint.

    HTTPS is required except on loopback, which exists so a developer can point
    at a server running on their own machine. Anything else would let a registry
    entry downgrade a credential-bearing request to plaintext.
    """
    url = _expand(str(raw or ""), where=f"MCP server {name!r} url").strip()
    if not url:
        raise McpRegistryError(f"MCP server {name!r}: url is required")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise McpRegistryError(f"MCP server {name!r}: url must be http(s), got {parts.scheme!r}")
    host = (parts.hostname or "").lower()
    if parts.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise McpRegistryError(
            f"MCP server {name!r}: refusing a plaintext http:// endpoint for {host!r}; "
            "the proxy attaches this deployment's credential to every request to it"
        )
    return url


def _parse_tools(raw: Any, *, name: str) -> frozenset[str]:
    """Validate one server's tool allowlist."""
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise McpRegistryError(f"MCP server {name!r}: tools must be a list of tool names")
    tools: set[str] = set()
    for entry in raw:
        tool = str(entry).strip()
        if not _TOOL_PATTERN.match(tool):
            raise McpRegistryError(f"MCP server {name!r}: invalid tool name {tool!r}")
        tools.add(tool)
    return frozenset(tools)


def _parse_server(raw: Any, *, index: int) -> McpServer:
    """Validate one ``servers:`` entry."""
    if not isinstance(raw, dict):
        raise McpRegistryError(f"MCP server #{index}: each entry must be a mapping")
    name = str(raw.get("name") or "").strip()
    if not NAME_PATTERN.match(name):
        raise McpRegistryError(
            f"MCP server #{index}: name {name!r} must match {NAME_PATTERN.pattern} — it becomes "
            "a URL path segment and the prefix of every tool name the model sees"
        )
    transport = str(raw.get("transport") or "http").strip().lower()
    if transport not in ("http", "streamable-http"):
        raise McpRegistryError(
            f"MCP server {name!r}: transport {transport!r} is not supported. Only remote "
            "streamable-HTTP servers can be offered; a stdio server is third-party code and "
            "has nowhere to run that is neither the closed sandbox nor the trusted gateway"
        )
    if "command" in raw:
        raise McpRegistryError(
            f"MCP server {name!r}: 'command' describes a stdio server, which cannot be "
            "offered here. It is third-party code, and the only places to run it are the "
            "sandbox that has no egress and the gateway that is trusted. Use a remote "
            "streamable-HTTP endpoint instead"
        )
    return McpServer(
        name=name,
        url=_parse_url(raw.get("url"), name=name),
        headers=_parse_headers(raw.get("headers"), name=name),
        tools=_parse_tools(raw.get("tools"), name=name),
        description=str(raw.get("description") or "").strip()[:400],
        default=bool(raw.get("default", False)),
    )


def load_registry(path: str | Path | None) -> McpRegistry:
    """Load the registry from YAML; a missing file means no servers.

    A deployment that configures nothing gets exactly today's behaviour, which
    is why absence is not an error: the neutral upstream ships no registry, and
    an agent job without MCP is a supported shape rather than a broken one.
    """
    if not path:
        return McpRegistry()
    config_path = Path(path)
    if not config_path.exists():
        logger.info(
            "agent_mcp_registry_absent",
            extra={"event": "agent_mcp_registry_absent", "path": str(config_path)},
        )
        return McpRegistry()

    try:
        document = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise McpRegistryError(f"could not read the MCP registry at {config_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise McpRegistryError(f"{config_path}: expected a mapping with a 'servers:' key")

    entries = document.get("servers") or []
    if not isinstance(entries, list):
        raise McpRegistryError(f"{config_path}: 'servers' must be a list")

    servers: dict[str, McpServer] = {}
    for index, entry in enumerate(entries):
        server = _parse_server(entry, index=index)
        if server.name in servers:
            raise McpRegistryError(f"{config_path}: duplicate MCP server name {server.name!r}")
        servers[server.name] = server

    unfiltered = sorted(name for name, server in servers.items() if server.unfiltered)
    if unfiltered:
        # Not an error: a small server with three read-only tools does not need
        # an allowlist. But an agent runs untrusted repository content, and a
        # server left unfiltered exposes whatever it exposes — including any
        # write tool it grows in a later release, with no config change to
        # notice. Saying so at startup is the cheapest place to catch that.
        logger.warning(
            "agent_mcp_server_unfiltered",
            extra={"event": "agent_mcp_server_unfiltered", "servers": unfiltered},
        )
    logger.info(
        "agent_mcp_registry_loaded",
        extra={
            "event": "agent_mcp_registry_loaded",
            "path": str(config_path),
            "servers": sorted(servers),
        },
    )
    return McpRegistry(servers=servers)


_cached: McpRegistry | None = None


def get_registry() -> McpRegistry:
    """Return the deployment's registry, loading it once.

    Cached because the composer resolves it on every ``/config`` call and the
    proxy on every MCP request. A malformed registry degrades to *no servers*
    rather than propagating out of whatever request happened to touch it first:
    the gateway serves models, and an unparseable optional file must not be
    able to take that down. The error is logged loudly and the composer then
    offers nothing, which is visible.
    """
    global _cached
    if _cached is None:
        from serving.config.distribution import resolve_config_path

        try:
            _cached = load_registry(resolve_config_path("mcp").path)
        except McpRegistryError:
            logger.error("agent_mcp_registry_invalid", exc_info=True)
            _cached = McpRegistry()
    return _cached


def reset_registry_cache() -> None:
    """Drop the cached registry (tests, and config reload)."""
    global _cached
    _cached = None


__all__ = [
    "NAME_PATTERN",
    "McpRegistry",
    "McpRegistryError",
    "McpServer",
    "get_registry",
    "load_registry",
    "reset_registry_cache",
]
