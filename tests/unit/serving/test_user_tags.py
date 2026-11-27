"""User tags: how a tag's limits combine and fold into an account's (``serving.user_tags``)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from serving import user_tags
from serving.accounting import AccountingPolicy, bind_accounting_policy
from serving.model_access import (
    ModelAccessDenial,
    is_model_outside_tag_scope,
    model_access_denial,
)
from serving.servers.concurrency import (
    UserConcurrencyLimiter,
    effective_user_concurrency,
    static_limits_provider,
)
from serving.user_tags import (
    TAG_ALLOWED_MODELS_KEY,
    TAG_MAX_CONCURRENT_KEY,
    USER_TAGS_KEY,
    combine_tag_limits,
    decode_tag_policies,
    ledger_amount,
    tag_context,
    tighten_identity,
    within_tag_cap,
)


def _policy(name, **limits):
    return {"name": name, "source": "auto", **limits}


@pytest.fixture
def cny_ledger():
    """A ledger kept in CNY at 7.2 per USD; the USD default afterwards."""
    bind_accounting_policy(
        AccountingPolicy(
            currency="CNY",
            usd_to_currency_rate=Decimal("7.2"),
            rate_date="2026-11-01",
            rate_source="test",
        )
    )
    try:
        yield
    finally:
        bind_accounting_policy(None)


class TestCombine:
    def test_the_strictest_of_each_limit_applies(self):
        limits = combine_tag_limits(
            [
                _policy("a", quota_daily_cost=5, max_concurrent_requests=8),
                _policy(
                    "b",
                    quota_daily_cost=Decimal("2.5"),
                    quota_monthly_cost=40,
                    max_concurrent_requests=3,
                ),
            ]
        )
        assert limits.names == ("a", "b")
        assert limits.quota_daily_cost == Decimal("2.5")
        assert limits.quota_monthly_cost == Decimal(40)
        assert limits.max_concurrent_requests == 3
        assert limits.allowed_models is None

    def test_allowlists_intersect_and_a_tag_without_one_restricts_nothing(self):
        limits = combine_tag_limits(
            [
                _policy("a", allowed_models=["m1", "m2", "m3"]),
                _policy("b", allowed_models=["m2", "m3", "m4"]),
                _policy("c"),
            ]
        )
        assert limits.allowed_models == ("m2", "m3")

    def test_disjoint_allowlists_allow_nothing(self):
        limits = combine_tag_limits(
            [_policy("a", allowed_models=["m1"]), _policy("b", allowed_models=["m2"])]
        )
        assert limits.allowed_models == ()

    def test_no_tags_set_no_limits(self):
        limits = combine_tag_limits([])
        assert limits == user_tags.TagLimits()


class TestLedgerAmount:
    def test_an_amount_in_the_ledger_currency_is_used_as_it_is(self):
        assert ledger_amount("4.5", "USD") == Decimal("4.5")
        assert ledger_amount(3, None) == Decimal(3)
        assert ledger_amount(None, "USD") is None

    def test_a_usd_amount_on_a_cny_ledger_comes_in_at_the_rate(self, cny_ledger):
        assert ledger_amount("10", "USD") == Decimal("72.0")
        assert ledger_amount("10", "CNY") == Decimal(10)

    def test_an_amount_in_an_unknown_unit_is_not_applied(self):
        assert ledger_amount("10", "EUR") is None


def _row(**fields):
    """An auth lookup's row, with the tag column the way asyncpg returns it."""
    policies = fields.pop("policies", None)
    return {
        "user_id": "u1",
        "quota_daily_cost_usd": Decimal("100"),
        "quota_daily_cost_amount": Decimal("100"),
        "quota_monthly_cost_usd": None,
        "quota_monthly_cost_amount": None,
        "tag_policies": None if policies is None else json.dumps(policies, default=str),
        **fields,
    }


class TestTightenIdentity:
    def test_a_row_without_the_column_is_left_alone(self):
        row = {"user_id": "u1", "quota_daily_cost_usd": Decimal("100")}
        assert tighten_identity(dict(row)) == row
        assert tighten_identity(None) is None

    def test_an_account_without_tags_keeps_its_limits(self):
        row = tighten_identity(_row())
        assert row[USER_TAGS_KEY] == []
        assert row[TAG_MAX_CONCURRENT_KEY] is None
        assert row[TAG_ALLOWED_MODELS_KEY] is None
        assert row["quota_daily_cost_usd"] == Decimal("100")
        assert "tag_policies" not in row

    def test_a_lower_tag_quota_tightens_both_columns(self):
        row = tighten_identity(
            _row(policies=[_policy("unattended", quota_daily_cost="5", quota_monthly_cost="50")])
        )
        assert row[USER_TAGS_KEY] == ["unattended"]
        assert row["quota_daily_cost_usd"] == Decimal("5")
        assert row["quota_daily_cost_amount"] == Decimal("5")
        # The account had no monthly quota: the tag's is the one now.
        assert row["quota_monthly_cost_usd"] == Decimal("50")
        assert row["quota_monthly_cost_amount"] == Decimal("50")

    def test_a_higher_tag_quota_never_loosens_the_account(self):
        row = tighten_identity(_row(policies=[_policy("generous", quota_daily_cost="500")]))
        assert row["quota_daily_cost_usd"] == Decimal("100")

    def test_an_unset_daily_quota_stands_for_the_default(self):
        row = _row(policies=[_policy("t", quota_daily_cost="2000")])
        row["quota_daily_cost_usd"] = row["quota_daily_cost_amount"] = None
        tightened = tighten_identity(row)
        # 2000 is above the 1000 USD default the unset quota stands for.
        assert tightened["quota_daily_cost_usd"] is None
        row = _row(policies=[_policy("t", quota_daily_cost="20")])
        row["quota_daily_cost_usd"] = row["quota_daily_cost_amount"] = None
        assert tighten_identity(row)["quota_daily_cost_usd"] == Decimal("20")

    def test_a_cny_ledger_tightens_the_native_amount_and_its_usd_equivalent(self, cny_ledger):
        row = _row(policies=[{**_policy("t", quota_daily_cost="36"), "accounting_currency": "CNY"}])
        row["quota_daily_cost_amount"] = Decimal("720")
        tightened = tighten_identity(row)
        assert tightened["quota_daily_cost_amount"] == Decimal("36")
        assert tightened["quota_daily_cost_usd"] == Decimal("5")

    def test_concurrency_and_models_ride_beside_the_quotas(self):
        row = tighten_identity(
            _row(
                policies=[
                    _policy("a", max_concurrent_requests=4, allowed_models=["m1", "m2"]),
                    _policy("b", max_concurrent_requests=2),
                ]
            )
        )
        assert row[TAG_MAX_CONCURRENT_KEY] == 2
        assert row[TAG_ALLOWED_MODELS_KEY] == ["m1", "m2"]
        assert tag_context(row) == {
            USER_TAGS_KEY: ["a", "b"],
            TAG_MAX_CONCURRENT_KEY: 2,
            TAG_ALLOWED_MODELS_KEY: ["m1", "m2"],
        }

    def test_folding_twice_changes_nothing_more(self):
        row = tighten_identity(_row(policies=[_policy("t", quota_daily_cost="5")]))
        again = tighten_identity(dict(row))
        assert again == row

    def test_a_malformed_column_reads_as_no_tags(self):
        assert decode_tag_policies("not json") == []
        assert decode_tag_policies('{"name": "x"}') == []
        assert decode_tag_policies([{"name": "x"}, "y"]) == [{"name": "x"}]


class TestConcurrency:
    @pytest.mark.parametrize(
        ("capacity", "cap", "expected"),
        [(8, None, 8), (8, 2, 2), (2, 8, 2), (0, 3, 3), (5, 0, 5)],
    )
    def test_a_tag_cap_is_a_ceiling(self, capacity, cap, expected):
        assert within_tag_cap(capacity, cap) == expected

    @pytest.mark.asyncio
    async def test_the_limiter_holds_the_role_default_and_an_override_to_the_cap(self):
        limiter = UserConcurrencyLimiter(static_limits_provider({"free": 4, "admin": 0}))
        granted = [(await limiter.try_acquire("u1", "free", False, tag_cap=2))[0] for _ in range(3)]
        assert granted == [True, True, False]
        # An unlimited admin becomes finite.
        granted = [(await limiter.try_acquire("a1", "admin", True, tag_cap=1))[0] for _ in range(2)]
        assert granted == [True, False]
        # A per-user override above the cap is held to it too.
        _, capacity, _ = await limiter.try_acquire("u2", "free", False, 10, tag_cap=3)
        assert capacity == 3

    @pytest.mark.asyncio
    async def test_the_reported_cap_is_the_enforced_one(self):
        user = {"user_id": "u1", "role": "free", TAG_MAX_CONCURRENT_KEY: 1}
        assert await effective_user_concurrency(user, None) == 1
        user = {"user_id": "u1", "role": "free", "max_concurrent_requests": 6}
        assert await effective_user_concurrency(user, None) == 6


class _Adapter:
    class config:
        id = "m1"


class _Route:
    published = True
    required_role = "free"
    admin_only = False

    def __init__(self):
        self.adapters = [(_Adapter(), 1)]


class TestModelAccess:
    def test_absent_means_no_tag_restricts_models(self):
        assert not is_model_outside_tag_scope("m1", {"role": "free"})
        assert not is_model_outside_tag_scope("m1", None)

    def test_a_listed_model_passes_and_another_is_outside(self):
        ctx = {"role": "free", TAG_ALLOWED_MODELS_KEY: ["m1"]}
        assert not is_model_outside_tag_scope("m1", ctx)
        assert is_model_outside_tag_scope("m2", ctx)
        assert is_model_outside_tag_scope("m1", {TAG_ALLOWED_MODELS_KEY: []})

    @pytest.mark.asyncio
    async def test_every_surface_reads_it_through_the_one_check(self):
        ctx = {"role": "free", TAG_ALLOWED_MODELS_KEY: ["other"]}
        assert await model_access_denial(_Route(), "m1", ctx) is ModelAccessDenial.OUTSIDE_TAG
        ctx = {"role": "free", TAG_ALLOWED_MODELS_KEY: ["m1"]}
        assert await model_access_denial(_Route(), "m1", ctx) is None
