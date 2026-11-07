"""Who did what to which object: the attribution an audit-log row carries.

``admin_audit_log`` grew two overloaded columns. ``admin_ip`` held a client IP
for some writers and the acting account's email or login name for others, and
``target_user_id`` held a user id for most writers but a provider name or a
model id for a few. Rows written from now on say both things explicitly:

- :class:`AuditActor` is who acted: its kind (:data:`ActorType`), the stable
  account id when an account acted (never an email or login name), and the
  trusted client address the request came from. That address goes to
  ``source_ip`` (NULL when the request had none we can trust) and is mirrored
  into ``admin_ip`` (``"unknown"`` then) for readers of the old column.
- :class:`AuditTarget` is the object acted on, typed. Only a ``user`` target
  is also written to ``target_user_id``, so filtering by user and the
  hard-delete erasure (``DELETE ... WHERE target_user_id = $1``) keep meaning
  exactly "rows about this user". A row about an account's API key targets
  that account: it is the affected user, and its erasure must take the row.

Rows written before this, and rows a caller still writes through the legacy
signature, leave the new columns NULL: actor "not recorded", ``source_ip``
NULL beside a raw ``admin_ip`` that may be an address, an email or a login
name, and an untyped ``target_user_id`` shown as stored. Nothing guesses a
legacy row's actor, source or target from the shape of those values.

Both objects are frozen and built per request; nothing here is process state.
The design record is
``docs/agents/specs/2026-10-25-audit-attribution-design.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

# Who can act. ``admin`` and ``user`` are an account signed in with a session
# token (on an admin route and on its own account's route respectively);
# ``admin_token`` is the shared ADMIN_TOKEN, which belongs to no account;
# ``anonymous`` is an unauthenticated request (registration); ``bootstrap`` is
# first-run setup, authorized by the one-time setup code rather than an
# account; ``system`` is the gateway itself, acting on no request.
ActorType = Literal["admin", "admin_token", "user", "anonymous", "bootstrap", "system"]

# What can be acted on. Add a type here before writing it. A model's routes,
# weights and offload policy are its own settings, so they target the model
# (the route stays in the details); an account's API keys target the account.
TargetType = Literal[
    "user",
    "model",
    "provider",
    "provider_key",
    "setting",
    "config",
    "alert_type",
    "auth_block",
    "signup_domain",
    "site_update",
    "broadcast",
]

ACTOR_TYPES: frozenset[str] = frozenset(get_args(ActorType))
TARGET_TYPES: frozenset[str] = frozenset(get_args(TargetType))

# What serving.utils.request_ip answers when the request had no address we can
# trust, and what ``admin_ip`` (NOT NULL) holds for such an actor.
UNKNOWN_SOURCE = "unknown"

# Actor kinds that are an account, and so must carry its id.
_ACCOUNT_ACTORS = frozenset({"admin", "user"})


@dataclass(frozen=True, slots=True)
class AuditActor:
    """Who performed an audited action.

    Attributes:
        type: The kind of actor (:data:`ActorType`).
        id: The acting account's stable user id, for ``admin`` and ``user``;
            None for every other kind, none of which is an account.
        source_ip: The trusted client address the request came from; None
            when it had none (``serving.utils.request_ip`` answered
            ``"unknown"``) or there was no request.
    """

    type: ActorType
    id: str | None = None
    source_ip: str | None = None

    def __post_init__(self) -> None:
        """Refuse an actor that would record something it does not know."""
        if self.type not in ACTOR_TYPES:
            raise ValueError(f"unknown audit actor type {self.type!r}")
        if self.type in _ACCOUNT_ACTORS:
            if not self.id:
                raise ValueError(f"a {self.type!r} audit actor needs the account's user id")
        elif self.id is not None:
            raise ValueError(f"a {self.type!r} audit actor has no account id")
        if self.source_ip in ("", UNKNOWN_SOURCE):
            object.__setattr__(self, "source_ip", None)

    @classmethod
    def admin(cls, user_id: str, source_ip: str | None) -> AuditActor:
        """An administrator account signed in with a session token."""
        return cls("admin", user_id, source_ip)

    @classmethod
    def admin_token(cls, source_ip: str | None) -> AuditActor:
        """A caller holding the shared ADMIN_TOKEN, which is no one's account."""
        return cls("admin_token", None, source_ip)

    @classmethod
    def user(cls, user_id: str, source_ip: str | None) -> AuditActor:
        """An account acting on itself through its own session."""
        return cls("user", user_id, source_ip)

    @classmethod
    def anonymous(cls, source_ip: str | None) -> AuditActor:
        """An unauthenticated request, such as a registration."""
        return cls("anonymous", None, source_ip)

    @classmethod
    def bootstrap(cls, source_ip: str | None) -> AuditActor:
        """First-run setup, authorized by the setup code rather than an account."""
        return cls("bootstrap", None, source_ip)

    @classmethod
    def system(cls) -> AuditActor:
        """The gateway itself, acting on no request."""
        return cls("system", None, None)


@dataclass(frozen=True, slots=True)
class AuditTarget:
    """The object an audited action was performed on.

    Attributes:
        type: The kind of object (:data:`TargetType`).
        id: Its identifier: a user id, model id, provider name, setting key...
    """

    type: TargetType
    id: str

    def __post_init__(self) -> None:
        """Refuse an untyped or anonymous target."""
        if self.type not in TARGET_TYPES:
            raise ValueError(f"unknown audit target type {self.type!r}")
        if not self.id:
            raise ValueError("an audit target needs an id")

    @property
    def user_id(self) -> str | None:
        """The id to store in ``target_user_id``: set only for a user target."""
        return self.id if self.type == "user" else None

    @classmethod
    def user(cls, user_id: str) -> AuditTarget:
        """A user account."""
        return cls("user", user_id)

    @classmethod
    def model(cls, model_id: str) -> AuditTarget:
        """A model, by its canonical id."""
        return cls("model", model_id)

    @classmethod
    def provider(cls, name: str) -> AuditTarget:
        """A provider, by its name."""
        return cls("provider", name)


@dataclass(frozen=True, slots=True)
class AuditColumns:
    """The attribution columns of one ``admin_audit_log`` row, ready to bind."""

    admin_ip: str
    target_user_id: str | None
    actor_type: str | None
    actor_id: str | None
    source_ip: str | None
    target_type: str | None
    target_id: str | None


def audit_columns(
    *,
    admin_ip: str | None,
    target_user_id: str | None,
    actor: AuditActor | None,
    target: AuditTarget | None,
) -> AuditColumns:
    """Resolve what a writer passed into the columns its row stores.

    With an *actor*, ``source_ip`` is its trusted address (NULL without one)
    and ``admin_ip`` mirrors it, ``"unknown"`` for none; passing *admin_ip*
    as well is refused, since the address comes from the actor alone.
    Without one (a legacy caller) *admin_ip* is stored raw as given, and the
    actor columns and ``source_ip`` stay NULL: "not recorded".

    With a *target*, ``target_user_id`` is its id when it is a user and NULL
    otherwise; passing *target_user_id* as well is refused. Without one, a
    legacy *target_user_id* is stored raw and left untyped: legacy writers
    put provider names there too, so its type is not assumed.

    Raises:
        ValueError: A legacy argument was passed beside the actor or target.
    """
    if actor is not None and admin_ip is not None:
        raise ValueError("pass the actor or a legacy admin_ip, not both")
    if target is not None and target_user_id is not None:
        raise ValueError("pass the target or a legacy target_user_id, not both")
    if actor is not None:
        source_ip = actor.source_ip
        admin_column = source_ip or UNKNOWN_SOURCE
    else:
        source_ip = None
        admin_column = admin_ip or UNKNOWN_SOURCE
    return AuditColumns(
        admin_ip=admin_column,
        target_user_id=target.user_id if target is not None else target_user_id,
        actor_type=actor.type if actor is not None else None,
        actor_id=actor.id if actor is not None else None,
        source_ip=source_ip,
        target_type=target.type if target is not None else None,
        target_id=target.id if target is not None else None,
    )
