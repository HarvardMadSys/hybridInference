"""User tags: groups of accounts, and the limits a tag holds its members to.

A tag (``user_tags``) names a group of accounts (``user_tag_members``). An
administrator adds and removes members by hand; a tag with an automatic rule
also gains every account that sent requests in at least ``min_active_hours``
of the last ``window_hours`` hours (:mod:`serving.admin.user_tag_detector`).
The gateway ships one such tag, :data:`UNATTENDED_TAG`: requests nearly
around the clock are a script or an agent running with nobody at the
keyboard.

**A tag's limits are ceilings.** A tag may set a daily and a monthly cost
quota, a model allowlist and a cap on concurrent requests. An account is held
to the strictest of its own limit and those of every tag it carries; allowlists
intersect. A tag therefore only ever narrows what an account may do, never
widens it: an automatic tag cannot loosen a limit an administrator set on the
account, and two tags never argue about which one wins.

Where the limits apply: the auth lookups carry the account's tags with each
tag's limits as one JSON array (``tag_policies``), and :func:`tighten_identity`
folds them into the row before anything reads it. The quotas are then the
account's quotas everywhere they are enforced -- the gate, the spend ledger's
admission, the balance endpoints -- on every door (an API key, an inference
grant, a request on a user's behalf). The concurrency cap and the allowlist
ride in the user context (:func:`tag_context`), read by
:mod:`serving.servers.concurrency` and :func:`serving.model_access.model_access_denial`.

An automatic tag stays until an administrator takes it away: requests that
are refused by the tag's own limits are not logged, so a rule re-read from the
log would lift the limits of exactly the accounts they stopped. Taking it away
leaves a ``dismissed`` row, and the detector gives the tag back only on
requests sent after that.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from serving.accounting import get_accounting_policy, money_decimal
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

logger = get_logger(__name__)

#: What a tag may be called: lowercase letters, digits, ``-`` and ``_``,
#: starting with a letter or digit, at most 32 characters. The table's CHECK
#: constraint is the same expression.
TAG_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: The built-in automatic tag (seeded by migration ``0023_user_tags``).
UNATTENDED_TAG = "unattended"

#: How an account came to carry a tag; ``dismissed`` carries none of its limits.
MemberSource = Literal["manual", "auto", "dismissed"]
MEMBER_SOURCES: tuple[MemberSource, ...] = ("manual", "auto", "dismissed")
#: The sources under which a tag applies.
APPLIED_SOURCES: tuple[MemberSource, ...] = ("manual", "auto")

#: The bounds of an automatic rule's window, in hours: a week at most.
MAX_WINDOW_HOURS = 168

#: User-context keys (see :func:`tag_context`).
USER_TAGS_KEY = "user_tags"
TAG_MAX_CONCURRENT_KEY = "tag_max_concurrent_requests"
#: The combined tag quotas a folded row keeps, in the ledger currency, for a
#: path that builds an account's quotas from something other than the row
#: itself (:func:`apply_tag_quotas`).
TAG_QUOTA_DAILY_KEY = "tag_quota_daily_cost"
TAG_QUOTA_MONTHLY_KEY = "tag_quota_monthly_cost"
#: Set when the account's tags restrict models; absent when none does. An empty
#: list is a real answer: tags whose allowlists share no model allow none.
TAG_ALLOWED_MODELS_KEY = "tag_allowed_models"

#: What the auth lookups name the tag column they add (see
#: :func:`serving.storage.user_tags.user_tags_join`). Folding removes it, so a
#: row is folded at most once.
_RAW_COLUMN = "tag_policies"

#: A key row with no daily quota stands for this many USD (``serving.quota``).
_DEFAULT_DAILY_QUOTA_USD = 1000.0


@dataclass(frozen=True, slots=True)
class TagLimits:
    """The limits an account's tags hold it to, combined.

    Amounts are in the ledger currency. ``None`` is "no tag sets this".
    ``allowed_models`` is sorted, and empty when the tags' allowlists share no
    model.
    """

    names: tuple[str, ...] = ()
    quota_daily_cost: Decimal | None = None
    quota_monthly_cost: Decimal | None = None
    max_concurrent_requests: int | None = None
    allowed_models: tuple[str, ...] | None = None


def is_valid_tag_name(name: object) -> bool:
    """Whether *name* is a tag name the table accepts."""
    return isinstance(name, str) and bool(TAG_NAME_PATTERN.fullmatch(name))


def ledger_amount(value: Any, currency: str | None) -> Decimal | None:
    """A tag's stored amount in today's ledger currency, or None.

    An amount is stored with the currency it was written in. One in the
    ledger's currency (or written before any was recorded) is used as it is;
    a USD amount on a ledger since moved to another currency comes in at the
    policy's fixed rate. Any other unit has no known rate: the amount is not
    applied, and the mismatch is logged for an administrator to re-enter it.
    """
    if value is None:
        return None
    try:
        amount = money_decimal(value)
    except ValueError:
        logger.error("user_tags: ignoring a stored amount that is not money: %r", value)
        return None
    policy = get_accounting_policy()
    if currency is None or currency == policy.currency:
        return _plain(amount)
    if currency == "USD":
        return _plain(policy.from_usd(amount))
    logger.error(
        "user_tags: ignoring an amount stored in %s on a %s ledger; re-enter it",
        currency,
        policy.currency,
    )
    return None


def _plain(amount: Decimal) -> Decimal:
    """*amount* without the column's trailing zeros: ``5``, not ``5.00000000`` or ``0E-8``."""
    return amount.quantize(Decimal(1)) if amount == amount.to_integral() else amount.normalize()


def _strictest(values: Iterable[Any]) -> Any:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def combine_tag_limits(policies: Iterable[Mapping[str, Any]]) -> TagLimits:
    """Combine the limits of every tag an account carries: the strictest of each.

    Each policy is one tag's row (or its JSON form): ``name``,
    ``quota_daily_cost``, ``quota_monthly_cost``, ``accounting_currency``,
    ``max_concurrent_requests`` and ``allowed_models``. Allowlists intersect;
    a tag without one restricts nothing.
    """
    names: list[str] = []
    daily: list[Decimal | None] = []
    monthly: list[Decimal | None] = []
    concurrency: list[int | None] = []
    allowed: set[str] | None = None
    for policy in policies:
        name = policy.get("name")
        if not isinstance(name, str):
            continue
        names.append(name)
        currency = policy.get("accounting_currency")
        daily.append(ledger_amount(policy.get("quota_daily_cost"), currency))
        monthly.append(ledger_amount(policy.get("quota_monthly_cost"), currency))
        cap = policy.get("max_concurrent_requests")
        concurrency.append(int(cap) if isinstance(cap, int) and not isinstance(cap, bool) else None)
        models = policy.get("allowed_models")
        if isinstance(models, list):
            listed = {model for model in models if isinstance(model, str)}
            allowed = listed if allowed is None else allowed & listed
    return TagLimits(
        names=tuple(sorted(names)),
        quota_daily_cost=_strictest(daily),
        quota_monthly_cost=_strictest(monthly),
        max_concurrent_requests=_strictest(concurrency),
        allowed_models=None if allowed is None else tuple(sorted(allowed)),
    )


def decode_tag_policies(raw: Any) -> list[dict[str, Any]]:
    """The tag column of an auth lookup as a list of policies.

    asyncpg hands JSONB back as text without a registered codec; amounts are
    parsed as decimals, never binary floats. A malformed value reads as no
    tags, logged: a column this module's own SQL builds is never expected to
    be one.
    """
    if raw is None:
        return []
    value = raw
    if isinstance(raw, (str, bytes)):
        try:
            value = json.loads(raw, parse_float=Decimal)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.error("user_tags: unreadable tag column %r", raw)
            return []
    if not isinstance(value, list):
        logger.error("user_tags: tag column is not a list: %r", value)
        return []
    return [item for item in value if isinstance(item, dict)]


def tighten_identity(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Fold the tag limits an auth lookup carried into its row, in place.

    The row's ``tag_policies`` column is replaced by ``user_tags``, the list
    of the account's tag names. Its quotas -- ``quota_daily_cost_usd`` and
    ``quota_monthly_cost_usd`` with their native ``*_amount`` beside them,
    where the lookup selected them -- become the strictest of the account's
    and its tags'; a daily quota the row leaves unset stands for the
    1000 USD default, as everywhere it is read. The concurrency cap and the
    allowlist are set beside them for :func:`tag_context`. A row without the
    column (a lookup that does not join tags) is returned unchanged.

    Returns:
        The same row, for chaining.
    """
    if row is None or _RAW_COLUMN not in row:
        return row
    row.pop("tag_monthly_quota", None)
    limits = combine_tag_limits(decode_tag_policies(row.pop(_RAW_COLUMN)))
    row[USER_TAGS_KEY] = list(limits.names)
    row[TAG_MAX_CONCURRENT_KEY] = limits.max_concurrent_requests
    row[TAG_ALLOWED_MODELS_KEY] = (
        None if limits.allowed_models is None else list(limits.allowed_models)
    )
    row[TAG_QUOTA_DAILY_KEY] = limits.quota_daily_cost
    row[TAG_QUOTA_MONTHLY_KEY] = limits.quota_monthly_cost
    apply_tag_quotas(row)
    return row


def apply_tag_quotas(row: dict[str, Any]) -> dict[str, Any]:
    """Hold the quotas in *row* to the tag quotas it carries, in place.

    *row* has the account's ``quota_daily_cost_usd`` / ``quota_monthly_cost_usd``
    (with their native ``*_amount`` where known) and the combined tag quotas
    (:data:`TAG_QUOTA_DAILY_KEY`, :data:`TAG_QUOTA_MONTHLY_KEY`, in the ledger
    currency). Each quota becomes the stricter of the two; a quota the row
    does not hold is left alone. Both columns of a tightened pair are set, so
    whichever one a gate reads -- the USD one on a USD ledger, the native one
    on another -- says the same.
    """
    from serving import quota

    policy = get_accounting_policy()
    daily = row.get(TAG_QUOTA_DAILY_KEY)
    if daily is not None and "quota_daily_cost_usd" in row:
        current = quota.native_limit(
            row,
            "quota_daily_cost_amount",
            "quota_daily_cost_usd",
            default_usd=_DEFAULT_DAILY_QUOTA_USD,
        )
        if current is None or daily < current:
            row["quota_daily_cost_amount"] = daily
            row["quota_daily_cost_usd"] = policy.to_usd(daily)
    monthly = row.get(TAG_QUOTA_MONTHLY_KEY)
    if monthly is not None and "quota_monthly_cost_usd" in row:
        current = quota.native_limit(row, "quota_monthly_cost_amount", "quota_monthly_cost_usd")
        if current is None or monthly < current:
            row["quota_monthly_cost_amount"] = monthly
            row["quota_monthly_cost_usd"] = policy.to_usd(monthly)
    return row


def tag_context(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """The user-context entries for the tags a folded row carries.

    ``user_tags`` always (empty for an account without tags, or a row from a
    lookup that does not join them); the concurrency cap, the allowlist and
    the combined quotas only when a tag sets them. The quotas are for a path
    that builds the account's quotas itself (:func:`apply_tag_quotas`); a
    lookup that selects them has already folded them in.
    """
    if not row:
        return {USER_TAGS_KEY: []}
    context: dict[str, Any] = {USER_TAGS_KEY: list(row.get(USER_TAGS_KEY) or [])}
    cap = row.get(TAG_MAX_CONCURRENT_KEY)
    if cap is not None:
        context[TAG_MAX_CONCURRENT_KEY] = int(cap)
    allowed = row.get(TAG_ALLOWED_MODELS_KEY)
    if allowed is not None:
        context[TAG_ALLOWED_MODELS_KEY] = list(allowed)
    for key in (TAG_QUOTA_DAILY_KEY, TAG_QUOTA_MONTHLY_KEY):
        if row.get(key) is not None:
            context[key] = row[key]
    return context


def within_tag_cap(capacity: int, cap: int | None) -> int:
    """A concurrency *capacity* held to a tag's *cap*; 0 is unlimited on both sides.

    A tag cap is a ceiling: it lowers a larger capacity and turns an unlimited
    one finite, and leaves a smaller one alone. A cap of 0 or less is no cap
    (the table refuses one, but a context is not the table).
    """
    if cap is None or cap <= 0:
        return capacity
    if capacity <= 0:
        return cap
    return min(capacity, cap)


def tag_concurrency_cap(user: Mapping[str, Any] | None) -> int | None:
    """The concurrency cap a user context's tags set, or None."""
    if not user:
        return None
    cap = user.get(TAG_MAX_CONCURRENT_KEY)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        return None
    return cap
