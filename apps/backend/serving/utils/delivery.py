"""Delivery transports registered by explicitly loaded backend extensions."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


@dataclass(frozen=True)
class EmailMessage:
    """Rendered account mail and optional scalar data for template transports."""

    to_email: str
    subject: str
    html_body: str
    text_body: str | None = None
    template: str = "notice"
    template_data: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        """Keep message metadata immutable after dispatch."""
        if self.template_data is not None:
            object.__setattr__(self, "template_data", MappingProxyType(dict(self.template_data)))


@dataclass(frozen=True)
class EmailProvider:
    """Synchronous configuration check and email transport."""

    is_configured: Callable[[], bool]
    send: Callable[[EmailMessage], bool]


@dataclass(frozen=True)
class SmsProvider:
    """Synchronous configuration check and verification-code transport."""

    is_configured: Callable[[], bool]
    send: Callable[[str, str], bool]


_email_providers: dict[str, EmailProvider] = {}
_sms_providers: dict[str, SmsProvider] = {}
_PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_]*", re.ASCII)


def _validate_provider(
    name: str, is_configured: Callable[..., object], send: Callable[..., object]
) -> None:
    if not _PROVIDER_NAME.fullmatch(name):
        raise ValueError(f"Invalid delivery provider name: {name!r}")
    if name in {"smtp", "none"}:
        raise ValueError(f"Delivery provider name is reserved: {name!r}")
    for callback in (is_configured, send):
        if (
            not callable(callback)
            or inspect.iscoroutinefunction(callback)
            or inspect.iscoroutinefunction(getattr(callback, "__call__", None))  # noqa: B004 - reject async callable objects
        ):
            raise TypeError(
                "Delivery providers require synchronous configuration and send callables"
            )


def _invalidate_config_choices() -> None:
    from serving.config.app_config_registry import invalidate_config_registry

    invalidate_config_registry()


def register_email_provider(
    name: str,
    *,
    is_configured: Callable[[], bool],
    send: Callable[[EmailMessage], bool],
) -> None:
    """Register an extension-owned email transport without replacing another."""
    _validate_provider(name, is_configured, send)
    if name in _email_providers:
        raise ValueError(f"Email provider already registered: {name!r}")
    _email_providers[name] = EmailProvider(is_configured=is_configured, send=send)
    _invalidate_config_choices()


def register_sms_provider(
    name: str,
    *,
    is_configured: Callable[[], bool],
    send: Callable[[str, str], bool],
) -> None:
    """Register an extension-owned verification transport without replacement."""
    _validate_provider(name, is_configured, send)
    if name in _sms_providers:
        raise ValueError(f"SMS provider already registered: {name!r}")
    _sms_providers[name] = SmsProvider(is_configured=is_configured, send=send)
    _invalidate_config_choices()


def email_provider_names() -> tuple[str, ...]:
    """Return the built-in and explicitly registered email provider choices."""
    return ("smtp", *sorted(_email_providers))


def sms_provider_names() -> tuple[str, ...]:
    """Return the disabled choice and explicitly registered SMS providers."""
    return ("none", *sorted(_sms_providers))


def get_email_provider(name: str) -> EmailProvider | None:
    """Look up an extension email provider without enabling a fallback."""
    return _email_providers.get(name)


def get_sms_provider(name: str) -> SmsProvider | None:
    """Look up an extension SMS provider without enabling a fallback."""
    return _sms_providers.get(name)
