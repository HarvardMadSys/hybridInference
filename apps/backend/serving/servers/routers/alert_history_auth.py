"""Credentials for the alert-history API: scoped readers and scoped writers.

Both kinds live in ``app_config`` as JSON lists, and both store only the
SHA-256 of each token, never the token::

    ALERT_HISTORY_READ_CREDENTIALS = [{"id": "agent-readonly-1",
        "sha256": "<64 hex>", "deployment_ids": ["example-deployment"],
        "target_environments": ["production"], "expires_at": "2027-01-31T00:00:00Z"}]

    ALERT_HISTORY_WRITER_CREDENTIALS = [{"id": "monitor-writer-1",
        "sha256": "<64 hex>", "source": "status_monitor",
        "source_namespace": "example-monitor", "deployment_id": "example-deployment",
        "target_environments": ["production", "staging"],
        "expires_at": "2027-01-31T00:00:00Z"}]

Everything a request may write or read is decided by the matching entry, not
by the request. A writer token is accepted only by the ingestion endpoint and a
reader token only by the history reads; neither is an admin credential, and
neither is the internal dispatch token, so the other routes refuse them.

**Fails closed.** A list that does not parse refuses every token of both kinds
(the admin path is unaffected). So does a configuration this process cannot
confirm is current: the table is re-read every 10 seconds, and when the last
successful read is older than :data:`MAX_CONFIG_AGE_SEC` a revoked token might
still look valid, so no history token is accepted until a read succeeds.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from serving.config import app_config
from serving.observability.alert_history.runtime import get_runtime
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.servers.routers.internal_auth import error
from serving.storage.alert_history_store import Scope

READ_SETTING = "ALERT_HISTORY_READ_CREDENTIALS"
WRITE_SETTING = "ALERT_HISTORY_WRITER_CREDENTIALS"
#: Longest a credential decision may rest on an unconfirmed configuration.
MAX_CONFIG_AGE_SEC = 30.0
#: Shorter tokens are refused before hashing; issued tokens are far longer.
MIN_TOKEN_LENGTH = 32
RESERVED_SOURCES = frozenset({"backend"})
#: Silence after which a source is shown stale, unless its credential says
#: otherwise. Also the backend's own, whose heartbeat is every 30 seconds.
DEFAULT_STALE_AFTER_SEC = 180
MAX_STALE_AFTER_SEC = 86_400

_LABEL = r"^[a-z0-9_.-]{1,64}$"
_ID = r"^[A-Za-z0-9_.:@-]{1,64}$"
_HEX = re.compile(r"^[0-9a-f]{64}$")


class _Credential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_ID)
    sha256: str
    expires_at: dt.datetime

    @field_validator("sha256")
    @classmethod
    def _hex(cls, value: str) -> str:
        value = value.strip().lower()
        if not _HEX.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value

    @field_validator("expires_at")
    @classmethod
    def _aware(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at needs an explicit offset")
        return value


class ReaderCredential(_Credential):
    """A read-only token's scope."""

    deployment_ids: tuple[Annotated[str, Field(pattern=_LABEL)], ...] = Field(
        min_length=1, max_length=16
    )
    target_environments: tuple[Annotated[str, Field(pattern=_LABEL)], ...] = Field(
        min_length=1, max_length=16
    )
    sources: tuple[Annotated[str, Field(pattern=r"^[a-z0-9_]{1,40}$")], ...] | None = Field(
        default=None, min_length=1, max_length=16
    )

    def scope(self) -> Scope:
        """The rows this token may read."""
        return Scope(
            deployment_ids=self.deployment_ids,
            target_environments=self.target_environments,
            sources=self.sources,
        )


class WriterCredential(_Credential):
    """A source token's binding."""

    source: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    source_namespace: str = Field(pattern=_LABEL)
    deployment_id: str = Field(pattern=_LABEL)
    target_environments: tuple[Annotated[str, Field(pattern=_LABEL)], ...] = Field(
        min_length=1, max_length=16
    )
    #: How long this source may be silent, per target environment, before the
    #: status endpoint calls it stale: its reporting cadence plus a margin
    #: (a 20-minute cron needs well over 1200). Bounded so a typo cannot hide
    #: a dead source for a week; the default suits a continuously running one.
    stale_after_sec: int = Field(default=DEFAULT_STALE_AFTER_SEC, ge=60, le=MAX_STALE_AFTER_SEC)

    @field_validator("source")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value in RESERVED_SOURCES:
            raise ValueError(f"source {value!r} is reserved for the gateway itself")
        return value


class CredentialsUnavailable(Exception):
    """The credential lists cannot be trusted right now; refuse every token."""


@dataclass(frozen=True)
class Credentials:
    """Both parsed lists."""

    readers: tuple[ReaderCredential, ...]
    writers: tuple[WriterCredential, ...]


_CACHE: dict[tuple[str, str], Credentials] = {}


def _parse_list(raw: str, model: type[_Credential]) -> tuple[Any, ...]:
    text = (raw or "").strip()
    if not text:
        return ()
    data = json.loads(text)
    if not isinstance(data, list) or len(data) > 64:
        raise ValueError("credentials must be a JSON list of at most 64 entries")
    return tuple(model.model_validate(item) for item in data)


def load_credentials() -> Credentials:
    """Parse both settings. Raises :class:`CredentialsUnavailable` to fail closed."""
    age = app_config.seconds_since_read()
    if age is not None and age > MAX_CONFIG_AGE_SEC:
        raise CredentialsUnavailable("configuration_stale")
    raw_read = app_config.config_value(READ_SETTING) or ""
    raw_write = app_config.config_value(WRITE_SETTING) or ""
    cached = _CACHE.get((raw_read, raw_write))
    if cached is not None:
        return cached
    try:
        readers = _parse_list(raw_read, ReaderCredential)
        writers = _parse_list(raw_write, WriterCredential)
    except (ValueError, ValidationError, TypeError) as exc:
        raise CredentialsUnavailable("configuration_invalid") from exc
    digests = [c.sha256 for c in (*readers, *writers)]
    ids = [c.id for c in (*readers, *writers)]
    if len(set(digests)) != len(digests) or len(set(ids)) != len(ids):
        raise CredentialsUnavailable("configuration_invalid")
    by_pair: dict[tuple[str, str], str] = {}
    for writer in writers:
        previous = by_pair.setdefault(
            (writer.source, writer.source_namespace), writer.deployment_id
        )
        if previous != writer.deployment_id:
            # Two credentials for one source key space must agree on whose it is.
            raise CredentialsUnavailable("configuration_invalid")
    credentials = Credentials(readers=readers, writers=writers)
    _CACHE.clear()
    _CACHE[(raw_read, raw_write)] = credentials
    return credentials


def token_digest(token: str) -> str:
    """SHA-256 hex of a presented token, as stored in the credential lists."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _match(token: str, candidates: tuple[_Credential, ...]) -> _Credential | None:
    digest = token_digest(token)
    found = None
    # Compare against every entry, so timing does not reveal the position.
    for candidate in candidates:
        if hmac.compare_digest(candidate.sha256, digest):
            found = candidate
    return found


def _bearer(authorization: str | None) -> str:
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:].strip()
    return ""


def _expired(credential: _Credential) -> bool:
    return credential.expires_at <= dt.datetime.now(dt.UTC)


@dataclass(frozen=True)
class ReaderIdentity:
    """Who is reading, and what they may see."""

    kind: str  # "admin" | "reader"
    principal: str
    scope: Scope


async def require_history_reader(
    request: Request,
    authorization: str | None = Header(None),
    op_store=Depends(get_operational_store),
) -> ReaderIdentity:
    """Admit an admin (full scope) or a scoped reader token. Writers are refused."""
    token = _bearer(authorization)
    if len(token) >= MIN_TOKEN_LENGTH:
        try:
            credentials = load_credentials()
        except CredentialsUnavailable:
            credentials = None
        if credentials is not None:
            reader = _match(token, credentials.readers)
            if reader is not None:
                if _expired(reader):
                    raise error(status.HTTP_401_UNAUTHORIZED, "unauthorized", "Token expired.")
                scoped = cast("ReaderCredential", reader)
                return ReaderIdentity("reader", scoped.id, scoped.scope())
            if _match(token, credentials.writers) is not None:
                raise error(
                    status.HTTP_403_FORBIDDEN,
                    "wrong_credential_kind",
                    "A history writer credential cannot read history.",
                )
        else:
            # Cannot tell a history token from anything else: only an admin
            # credential, checked on its own terms below, may proceed.
            try:
                admin = await verify_admin_access(request, authorization, op_store)
            except HTTPException:
                raise error(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "history_auth_unavailable",
                    "History credentials cannot be verified right now; retry later.",
                ) from None
            return ReaderIdentity("admin", admin, Scope())
    admin = await verify_admin_access(request, authorization, op_store)
    return ReaderIdentity("admin", admin, Scope())


def require_history_writer(authorization: str | None = Header(None)) -> WriterCredential:
    """Admit only a configured, unexpired writer token."""
    runtime = get_runtime()
    if runtime is None or not runtime.enabled:
        raise error(
            status.HTTP_404_NOT_FOUND,
            "alert_history_not_configured",
            "This deployment does not accept alert history.",
        )
    try:
        credentials = load_credentials()
    except CredentialsUnavailable:
        raise error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "history_auth_unavailable",
            "History credentials cannot be verified right now; retry later.",
        ) from None
    if not credentials.writers:
        raise error(
            status.HTTP_404_NOT_FOUND,
            "alert_history_not_configured",
            "This deployment does not accept alert history.",
        )
    token = _bearer(authorization)
    if len(token) < MIN_TOKEN_LENGTH:
        raise error(status.HTTP_401_UNAUTHORIZED, "unauthorized", "Invalid history token.")
    writer = _match(token, credentials.writers)
    if writer is None:
        if _match(token, credentials.readers) is not None:
            raise error(
                status.HTTP_403_FORBIDDEN,
                "wrong_credential_kind",
                "A history reader credential cannot write history.",
            )
        raise error(status.HTTP_401_UNAUTHORIZED, "unauthorized", "Invalid history token.")
    if _expired(writer):
        raise error(status.HTTP_401_UNAUTHORIZED, "unauthorized", "Token expired.")
    return cast("WriterCredential", writer)
