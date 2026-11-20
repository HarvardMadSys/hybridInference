"""Request and response models of the organization routes.

Shared by the organization console (``/user/organization``) and the Admin
routes (``/admin/organizations``): an organization is shown in one shape to
its own administrators and to the platform administrator.

No model here has a field for request content or for the value of an API key:
an organization administrator sees how much its members use, never what they
send.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, field_validator

from serving.accounting_api import MoneyInput, MoneyResponse

#: The most a monthly limit can be: what ``DECIMAL(14, 4)`` holds for the
#: organization, and ``DECIMAL(10, 4)`` for one member.
ORGANIZATION_LIMIT_MAX_USD = Decimal("9999999999.9999")
MEMBER_LIMIT_MAX_USD = Decimal("999999.9999")

Period = Literal["1d", "7d", "30d"]
MemberRole = Literal["member", "admin"]


def _without_outer_spaces(value: Any) -> Any:
    """A name as it is stored; a name of spaces alone is then too short to be one."""
    return value.strip() if isinstance(value, str) else value


class OrganizationRef(BaseModel):
    """The organization an account belongs to, on the account's own profile."""

    id: str
    name: str
    role: MemberRole


class OrganizationConnection(BaseModel):
    """One SSO connection of an organization, without its secret or client id.

    ``available`` is False when ``SSO_PROVIDERS`` has no entry with this id any
    more; every other field is then unset.
    """

    provider_id: str
    available: bool
    display_name: str | None = None
    type: str | None = None
    domains: list[str] = Field(default_factory=list)
    enabled: bool = False
    auto_create: bool = False
    require_approval: bool = False


class OrganizationAdministrator(BaseModel):
    """A member that has the administrator role."""

    user_id: str
    email: str | None = None
    user_name: str | None = None
    last_login_at: datetime | None = None


class OrganizationResponse(MoneyResponse):
    """One organization: the record, its connections, its administrators and its limits."""

    id: str
    name: str
    created_at: datetime
    member_count: int
    #: NULL is no limit.
    monthly_limit_usd: float | None = None
    monthly_limit: Decimal | None = None
    #: What a member gets that the organization set no limit for; NULL is none.
    #: Not an administrator, of the organization or of the platform.
    default_member_monthly_limit_usd: float | None = None
    default_member_monthly_limit: Decimal | None = None
    #: What the organization's counter holds for this UTC month.
    spent_month_usd: float = 0.0
    spent_month: Decimal | None = None
    connections: list[OrganizationConnection] = Field(default_factory=list)
    administrators: list[OrganizationAdministrator] = Field(default_factory=list)


class ListOrganizationsResponse(BaseModel):
    """Every organization of the deployment."""

    organizations: list[OrganizationResponse]


class CreateOrganizationRequest(MoneyInput):
    """Make an organization from one SSO connection or more.

    The monthly limit is either the USD ``monthly_limit_usd`` or the native
    ``monthly_limit`` with the accounting ``currency``, never both.
    """

    usd_places: ClassVar[dict[str, int]] = {"monthly_limit_usd": 4}

    name: str = Field(..., min_length=1, max_length=80)
    provider_ids: list[str] = Field(..., min_length=1, max_length=20)

    _name = field_validator("name", mode="before")(_without_outer_spaces)
    monthly_limit_usd: Decimal | None = Field(
        default=None, ge=0, le=ORGANIZATION_LIMIT_MAX_USD, decimal_places=4
    )
    monthly_limit: Decimal | None = Field(default=None, ge=0, exclude=True)


class UpdateOrganizationRequest(MoneyInput):
    """Change the name or the monthly limit; a field that is not sent stays as it is.

    ``null`` for the monthly limit clears it.
    """

    usd_places: ClassVar[dict[str, int]] = {"monthly_limit_usd": 4}

    name: str | None = Field(default=None, min_length=1, max_length=80)
    monthly_limit_usd: Decimal | None = Field(
        default=None, ge=0, le=ORGANIZATION_LIMIT_MAX_USD, decimal_places=4
    )
    monthly_limit: Decimal | None = Field(default=None, ge=0, exclude=True)

    _name = field_validator("name", mode="before")(_without_outer_spaces)


class OrganizationCreatedResponse(BaseModel):
    """An organization that was just made, and how many accounts became its members."""

    organization: OrganizationResponse
    members_added: int


class ConnectionAttachedResponse(BaseModel):
    """An attached SSO connection, and how many accounts became members with it."""

    organization: OrganizationResponse
    members_added: int


class SetMemberRoleRequest(BaseModel):
    """Give a member the administrator role, or take it back."""

    role: MemberRole
    password: str | None = Field(
        None,
        description="The signed-in administrator's password: needed to give the role",
    )


class SetMonthlyLimitRequest(MoneyInput):
    """A monthly limit, or ``null`` for none.

    Either the USD ``monthly_limit_usd`` or the native ``monthly_limit`` with
    the accounting ``currency``, never both.
    """

    usd_places: ClassVar[dict[str, int]] = {"monthly_limit_usd": 4}

    monthly_limit_usd: Decimal | None = Field(
        default=None, ge=0, le=MEMBER_LIMIT_MAX_USD, decimal_places=4
    )
    monthly_limit: Decimal | None = Field(default=None, ge=0, exclude=True)


class OrganizationMessageResponse(BaseModel):
    """The outcome of an action that returns nothing else."""

    message: str


class OrganizationMember(MoneyResponse):
    """One member, with its usage in the requested period.

    ``status`` is the account's status. ``removed`` is True for a member the
    organization removed: only such a member is the organization's to restore.
    ``protected`` is True for an administrator, of the organization or of the
    platform: the organization sets no limit of it and does not remove it.

    Two parties can limit a member, and the lower limit applies:

    - its organization, with the limit it set for the member
      (``own_monthly_limit``), else with its default;
    - the platform administrator, with the monthly quota of the account
      (``platform_monthly_limit``), which the organization can read and not
      change.

    ``monthly_limit`` is the limit that applies, and ``monthly_limit_source``
    says which one it is: ``member``, ``default``, ``platform``, or ``none``
    for no limit.
    """

    user_id: str
    email: str | None = None
    user_name: str | None = None
    role: MemberRole
    protected: bool = False
    status: str
    removed: bool = False
    joined_at: datetime
    last_login_at: datetime | None = None
    last_request_at: datetime | None = None
    requests: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost: Decimal | None = None
    spent_month_usd: float = 0.0
    spent_month: Decimal | None = None
    monthly_limit_usd: float | None = None
    monthly_limit: Decimal | None = None
    monthly_limit_source: Literal["member", "default", "platform", "none"] = "none"
    own_monthly_limit_usd: float | None = None
    own_monthly_limit: Decimal | None = None
    platform_monthly_limit_usd: float | None = None
    platform_monthly_limit: Decimal | None = None


class ListMembersResponse(BaseModel):
    """One page of members and how many match the filters."""

    total: int
    members: list[OrganizationMember]


class OrganizationAuditEntry(BaseModel):
    """One management action of an organization.

    ``actor`` is ``platform_administrator`` for a platform administrator,
    whose account an organization is not shown; ``member`` otherwise, with the
    account in ``actor_user_id`` and ``actor_email``.
    """

    id: int
    timestamp: datetime
    action: str
    category: Literal["membership", "limits", "organization"]
    actor: Literal["platform_administrator", "member", "system"]
    actor_user_id: str | None = None
    actor_email: str | None = None
    target_type: str | None = None
    target_user_id: str | None = None
    target_email: str | None = None
    details: dict | None = None


class ListOrganizationAuditResponse(BaseModel):
    """One page of an organization's audit log, newest first."""

    total: int
    entries: list[OrganizationAuditEntry]


class UsagePoint(MoneyResponse):
    """One point of a usage series: a UTC day, or an hour for the ``1d`` period."""

    bucket: datetime
    cost_usd: float = 0.0
    cost: Decimal | None = None
    requests: int = 0
    total_tokens: int = 0
    members_with_requests: int = 0


class ModelUsage(MoneyResponse):
    """What one model was used for in a period."""

    model_id: str
    requests: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    cost: Decimal | None = None


class PeriodTotals(MoneyResponse):
    """Spend and requests of a period, for the change against the period before."""

    cost_usd: float = 0.0
    cost: Decimal | None = None
    requests: int = 0


class OrganizationOverviewResponse(MoneyResponse):
    """The organization's usage in a period.

    ``previous`` is the period of the same length before this one, for ``1d``
    and ``7d``. ``spent_month_usd`` is the organization's counter for this UTC
    month, the amount its limit is checked against.
    """

    period: Period
    bucket: Literal["day", "hour"]
    cost_usd: float = 0.0
    cost: Decimal | None = None
    requests: int = 0
    total_tokens: int = 0
    members_with_requests: int = 0
    member_count: int = 0
    previous: PeriodTotals | None = None
    spent_month_usd: float = 0.0
    spent_month: Decimal | None = None
    monthly_limit_usd: float | None = None
    monthly_limit: Decimal | None = None
    series: list[UsagePoint] = Field(default_factory=list)
    models: list[ModelUsage] = Field(default_factory=list)


class OrganizationRequest(MoneyResponse):
    """One request of a member: who, with what, how much. Never its content."""

    id: int
    timestamp: datetime
    user_id: str
    email: str | None = None
    user_name: str | None = None
    project_name: str | None = None
    key_name: str | None = None
    model_id: str
    status_code: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    cost: Decimal | None = None


class ListOrganizationRequestsResponse(BaseModel):
    """One page of requests, newest first; ``next_before`` asks for the page after it."""

    requests: list[OrganizationRequest]
    next_before: str | None = None


class ModelUsageResponse(BaseModel):
    """The models the organization's members used in a period."""

    period: Period
    models: list[ModelUsage]


class MemberDetailResponse(BaseModel):
    """One member: its row, its spend for each of the last 30 days, its models, its last requests."""

    member: OrganizationMember
    days: list[UsagePoint]
    models: list[ModelUsage]
    requests: list[OrganizationRequest]
