"""``quota.first_refusal`` and ``refusal_payload``: which limit ran out, and the 429 for it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from serving import quota


def test_the_month_starts_again_on_the_first_including_in_december():
    assert quota.next_utc_month_start(datetime(2026, 11, 30, 23, 59, tzinfo=UTC)) == datetime(
        2026, 12, 1, tzinfo=UTC
    )
    assert quota.next_utc_month_start(datetime(2026, 12, 31, 12, tzinfo=UTC)) == datetime(
        2027, 1, 1, tzinfo=UTC
    )


def test_nothing_spent_is_no_refusal():
    assert (
        quota.first_refusal(
            account_quota_usd=10,
            account_spent_usd=0,
            key_budget_daily_usd=5,
            key_budget_monthly_usd=50,
        )
        is None
    )


@pytest.mark.parametrize(
    ("spent", "expected"),
    [
        # key today, key month, account today -> the first limit that does not fit
        ((5, 50, 10), ("key", "day")),
        ((1, 50, 10), ("key", "month")),
        ((1, 1, 10), ("account", "day")),
        ((4.99, 49.99, 9.99), None),
    ],
)
def test_the_narrowest_limit_is_reported_first(spent, expected):
    key_today, key_month, account = spent

    refusal = quota.first_refusal(
        account_quota_usd=10,
        account_spent_usd=account,
        key_budget_daily_usd=5,
        key_budget_monthly_usd=50,
        key_spent_today_usd=key_today,
        key_spent_month_usd=key_month,
    )

    assert (None if refusal is None else (refusal.scope, refusal.window)) == expected


def test_a_key_with_no_budget_is_held_only_to_its_account():
    refusal = quota.first_refusal(
        account_quota_usd=10, account_spent_usd=1, key_spent_today_usd=1e6
    )

    assert refusal is None


def test_the_account_body_is_the_old_body_plus_its_scope():
    old, old_headers = quota.exceeded_payload(quota_usd=10, spent_usd=10)
    new, new_headers = quota.refusal_payload(quota.BudgetRefusal("account", "day", 10, 10))

    assert {k: v for k, v in new.items() if k not in ("code", "scope", "window")}.keys() == (
        old.keys()
    )
    assert (new["code"], new["scope"], new["window"]) == ("budget_exceeded", "account", "day")
    assert new["error"] == old["error"]
    assert new_headers.keys() == old_headers.keys()


def test_a_key_refusal_names_the_key_by_its_label_or_prefix():
    named, _ = quota.refusal_payload(
        quota.BudgetRefusal("key", "day", 5, 5, key_prefix="hyi-abc", key_label='"ci"')
    )
    unnamed, _ = quota.refusal_payload(
        quota.BudgetRefusal("key", "month", 5, 5, key_prefix="hyi-abc")
    )

    assert named["message"].startswith('API key "ci" reached its daily budget of $5.00')
    assert unnamed["message"].startswith("API key hyi-abc reached its monthly budget")
    assert unnamed["key_prefix"] == "hyi-abc"


@pytest.mark.parametrize(
    ("monthly_quota", "spent_month", "expected"),
    [
        (None, 1e6, None),
        (30, 29.98, None),
        (30, 30, ("account", "month")),
        (0, 0, ("account", "month")),
    ],
)
def test_the_accounts_monthly_quota_is_checked_last(monthly_quota, spent_month, expected):
    refusal = quota.first_refusal(
        account_quota_usd=10,
        account_spent_usd=1,
        account_quota_monthly_usd=monthly_quota,
        account_spent_month_usd=spent_month,
    )

    assert (None if refusal is None else (refusal.scope, refusal.window)) == expected


def test_the_accounts_monthly_body_has_the_daily_fields_and_renews_next_month():
    daily, daily_headers = quota.refusal_payload(quota.BudgetRefusal("account", "day", 10, 10))
    monthly, headers = quota.refusal_payload(
        quota.BudgetRefusal("account", "month", 30, 30), contact_email="ops@example.test"
    )

    assert monthly.keys() == daily.keys()
    assert monthly["error"] == "Monthly cost quota exceeded"
    assert (monthly["scope"], monthly["window"]) == ("account", "month")
    assert monthly["reset_at"] == quota.next_utc_month_start().isoformat()
    assert "ops@example.test" in monthly["message"]
    assert headers.keys() == daily_headers.keys()
