"""A paid request must never slip past durable cumulative budget accounting."""

from __future__ import annotations

import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal, localcontext

import pytest

from benchmark.nimbus.budget import (
    AttemptConflict,
    BudgetConfigurationError,
    BudgetExceeded,
    BudgetLedger,
    PricingProvenance,
    TokenPrices,
)

PRICES = TokenPrices("2", "0.2", "3")
ONE_YUAN_PER_TOKEN = TokenPrices("1000000", "1000000", "1000000")


def reserve(ledger, attempt_id="attempt-1", **overrides):
    terms = {
        "attempt_id": attempt_id,
        "run_id": "run-1",
        "prices": ONE_YUAN_PER_TOKEN,
        "input_tokens_upper_bound": 1,
        "max_output_tokens": 0,
    }
    return ledger.reserve(**(terms | overrides))


def _concurrent_reserve(args):
    path, index = args
    ledger = BudgetLedger(path, "concurrent", "10")
    try:
        reserve(ledger, f"attempt-{index}")
        return True
    except BudgetExceeded:
        return False


def _concurrent_dispatch(args):
    path, _index = args
    ledger = BudgetLedger(path, "concurrent", "10")
    try:
        ledger.mark_dispatched("shared-attempt")
        return True
    except AttemptConflict:
        return False


def _crash_after_dispatch(path, dispatch):
    ledger = BudgetLedger(path, "crash", "1")
    reserve(ledger)
    if dispatch:
        ledger.mark_dispatched("attempt-1")
    os._exit(7)


@pytest.fixture
def ledger(tmp_path):
    return BudgetLedger(tmp_path / "budget.sqlite", "campaign", "10")


def test_quote_uses_total_input_cached_subset_and_output():
    assert PRICES.quote(
        input_tokens=1_000_000, output_tokens=100_000, cached_input_tokens=200_000
    ) == Decimal("1.94")
    assert PRICES.reserve_quote(
        input_tokens_upper_bound=1_000_000, max_output_tokens=100_000
    ) == Decimal("2.3")
    unusual_prices = TokenPrices("1", "2", "3")
    assert unusual_prices.reserve_quote(
        input_tokens_upper_bound=1_000_000, max_output_tokens=0
    ) == Decimal("2")


def test_nanoyuan_rounding_and_decimal_context_do_not_underreserve(tmp_path):
    tiny_prices = TokenPrices("0.00000001", "0", "0")
    with localcontext() as context:
        context.prec = 2
        assert tiny_prices.quote(input_tokens=1, output_tokens=0) == Decimal("0.000000001")
        assert TokenPrices("123.456789012345", "0", "0").quote(
            input_tokens=1_000_000, output_tokens=0
        ) == Decimal("123.456789013")
    tiny = BudgetLedger(tmp_path / "tiny.sqlite", "tiny", "0.0000000019")
    assert tiny.snapshot()["cap_cny"] == "0.000000001"
    reserve(tiny, prices=tiny_prices)
    with pytest.raises(BudgetExceeded):
        reserve(tiny, "attempt-2", prices=tiny_prices)


@pytest.mark.parametrize("value", [0.1, True, "NaN", "Infinity", "-1", "nonsense"])
def test_prices_reject_inexact_or_invalid_amounts(value):
    with pytest.raises(ValueError):
        TokenPrices(value, "0", "0")


@pytest.mark.parametrize("value", [-1, 1.5, True, 1 << 63])
def test_invalid_usage_does_not_mutate_ledger(ledger, value):
    with pytest.raises(ValueError):
        reserve(ledger, input_tokens_upper_bound=value)
    assert ledger.export_entries() == []


def test_inconsistent_cached_input_is_rejected(ledger):
    reserve(ledger)
    ledger.mark_dispatched("attempt-1")
    with pytest.raises(ValueError, match="cannot exceed"):
        ledger.settle("attempt-1", input_tokens=1, output_tokens=0, cached_input_tokens=2)
    assert ledger.snapshot()["outstanding_reserved_cny"] == "1.000000000"


def test_campaign_and_original_cap_cannot_be_changed_or_reset(ledger):
    reserve(ledger)
    assert BudgetLedger(ledger.path, "campaign", Decimal("10.0")).snapshot() == ledger.snapshot()
    for campaign, cap in [("another", "10"), ("campaign", "1000"), ("campaign", "9")]:
        with pytest.raises(BudgetConfigurationError):
            BudgetLedger(ledger.path, campaign, cap)
    assert ledger.snapshot()["outstanding_reserved_cny"] == "1.000000000"


def test_ledger_rejects_nonpersistent_or_sub_nanoyuan_cap(tmp_path):
    with pytest.raises(BudgetConfigurationError):
        BudgetLedger(":memory:", "campaign", "1")
    with pytest.raises(BudgetConfigurationError):
        BudgetLedger(tmp_path / "zero.sqlite", "campaign", "0.0000000001")


def test_reservation_idempotency_preserves_one_liability_and_one_event(ledger):
    first = reserve(ledger)
    second = reserve(ledger, prices=TokenPrices("1000000.0", "1000000.00", "1000000.000"))
    assert first["created"] is True
    assert second["created"] is False
    assert first | {"created": False} == second
    assert ledger.snapshot()["outstanding_reserved_cny"] == "1.000000000"
    assert [event["event"] for event in ledger.export_entries()[0]["events"]] == ["reserved"]


@pytest.mark.parametrize(
    "changed",
    [
        {"run_id": "another-run"},
        {"input_tokens_upper_bound": 2},
        {"max_output_tokens": 1},
        {"prices": TokenPrices("1", "0", "0")},
    ],
)
def test_attempt_collision_refuses_different_reservation_terms(ledger, changed):
    reserve(ledger)
    with pytest.raises(AttemptConflict):
        reserve(ledger, **changed)
    assert ledger.snapshot()["outstanding_reserved_cny"] == "1.000000000"


def test_paid_and_outstanding_reservations_both_consume_cap(ledger):
    reserve(ledger, input_tokens_upper_bound=8)
    ledger.mark_dispatched("attempt-1")
    ledger.settle("attempt-1", input_tokens=6, output_tokens=0)
    reserve(ledger, "attempt-2", input_tokens_upper_bound=4)
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "attempt-3")
    assert ledger.snapshot()["spent_cny"] == "6.000000000"
    assert ledger.snapshot()["outstanding_reserved_cny"] == "4.000000000"
    assert ledger.snapshot()["remaining_cny"] == "0.000000000"


def test_dispatch_claim_is_exactly_once_and_settlement_is_idempotent(ledger):
    reserve(ledger, input_tokens_upper_bound=3)
    with pytest.raises(AttemptConflict):
        ledger.settle("attempt-1", input_tokens=1, output_tokens=0)
    ledger.mark_dispatched("attempt-1")
    with pytest.raises(AttemptConflict):
        ledger.mark_dispatched("attempt-1")
    result = ledger.settle("attempt-1", input_tokens=1, output_tokens=0)
    assert ledger.settle("attempt-1", input_tokens=1, output_tokens=0) == result
    with pytest.raises(AttemptConflict):
        ledger.settle("attempt-1", input_tokens=1, output_tokens=0, cached_input_tokens=1)
    assert reserve(ledger, input_tokens_upper_bound=3)["status"] == "settled"
    assert ledger.snapshot()["spent_cny"] == "1.000000000"
    assert ledger.snapshot()["outstanding_reserved_cny"] == "0.000000000"


def test_unknown_attempt_keeps_full_hold_across_restart_then_can_reconcile(ledger):
    reserve(ledger, input_tokens_upper_bound=10)
    ledger.mark_dispatched("attempt-1")
    unknown = ledger.mark_unknown("attempt-1")
    assert ledger.mark_unknown("attempt-1") == unknown
    reopened = BudgetLedger(ledger.path, "campaign", "10")
    assert reopened.snapshot()["available_cny"] == "0.000000000"
    with pytest.raises(BudgetExceeded):
        reserve(reopened, "attempt-2")
    with pytest.raises(AttemptConflict):
        reopened.cancel_before_dispatch("attempt-1")
    reopened.settle("attempt-1", input_tokens=2, output_tokens=0)
    assert reopened.snapshot()["available_cny"] == "8.000000000"


@pytest.mark.parametrize("dispatch", [False, True])
def test_crashed_process_never_releases_reserved_or_dispatched_liability(tmp_path, dispatch):
    path = tmp_path / "crash.sqlite"
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_after_dispatch, args=(path, dispatch)
    )
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("crash simulation did not finish")
    assert process.exitcode == 7
    ledger = BudgetLedger(path, "crash", "1")
    assert ledger.snapshot()["outstanding_reserved_cny"] == "1.000000000"
    assert ledger.export_entries()[0]["status"] == ("dispatched" if dispatch else "reserved")
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "attempt-after-crash")


def test_only_explicit_predispatch_cancellation_refunds(ledger):
    reserve(ledger)
    cancelled = ledger.cancel_before_dispatch("attempt-1")
    assert ledger.cancel_before_dispatch("attempt-1") == cancelled
    assert ledger.snapshot()["outstanding_reserved_cny"] == "0.000000000"
    assert reserve(ledger)["status"] == "cancelled"
    with pytest.raises(AttemptConflict):
        ledger.mark_dispatched("attempt-1")
    with pytest.raises(AttemptConflict):
        ledger.settle("attempt-1", input_tokens=0, output_tokens=0)
    reserve(ledger, "attempt-2")
    ledger.mark_dispatched("attempt-2")
    with pytest.raises(AttemptConflict):
        ledger.cancel_before_dispatch("attempt-2")


@pytest.mark.parametrize("known_cost", [2, 12])
def test_over_reserve_liability_is_recorded_and_all_future_dispatch_is_blocked(ledger, known_cost):
    reserve(ledger)
    reserve(ledger, "queued-attempt")
    ledger.mark_dispatched("attempt-1")
    result = ledger.settle("attempt-1", input_tokens=known_cost, output_tokens=0)
    assert result["reservation_exceeded"] is True
    snapshot = ledger.snapshot()
    assert Decimal(snapshot["spent_cny"]) == known_cost
    assert snapshot["blocked"] is True
    assert snapshot["available_cny"] == "0.000000000"
    assert snapshot["cap_exceeded"] is (known_cost == 12)
    assert Decimal(snapshot["remaining_cny"]) == 9 - known_cost
    reopened = BudgetLedger(ledger.path, "campaign", "10")
    with pytest.raises(BudgetExceeded):
        reserve(reopened, "another-attempt")
    with pytest.raises(BudgetExceeded):
        reopened.mark_dispatched("queued-attempt")
    assert reopened.settle("attempt-1", input_tokens=known_cost, output_tokens=0) == result


def test_concurrent_processes_cannot_overspend_same_campaign(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        accepted = list(pool.map(_concurrent_reserve, [(path, index) for index in range(24)]))
    assert sum(accepted) == 10
    ledger = BudgetLedger(path, "concurrent", "10")
    assert len(ledger.export_entries()) == 10
    assert ledger.snapshot()["committed_cny"] == "10.000000000"


def test_concurrent_processes_cannot_dispatch_one_reservation_twice(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    ledger = BudgetLedger(path, "concurrent", "10")
    reserve(ledger, "shared-attempt")
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        dispatched = list(pool.map(_concurrent_dispatch, [(path, index) for index in range(12)]))
    assert sum(dispatched) == 1
    assert [event["event"] for event in ledger.export_entries()[0]["events"]] == [
        "reserved",
        "dispatched",
    ]


def test_export_is_json_safe_scoped_and_contains_audit_events(ledger):
    reserve(ledger)
    ledger.mark_dispatched("attempt-1")
    ledger.settle("attempt-1", input_tokens=0, output_tokens=0)
    reserve(ledger, "attempt-2", run_id="run-2")
    exported = ledger.export_entries(run_id="run-1")
    assert [entry["attempt_id"] for entry in exported] == ["attempt-1"]
    assert [event["event"] for event in exported[0]["events"]] == [
        "reserved",
        "dispatched",
        "settled",
    ]
    assert json.loads(json.dumps(exported)) == exported
    assert json.loads(json.dumps(ledger.snapshot())) == ledger.snapshot()
    assert exported[0]["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
    }
    with pytest.raises(ValueError):
        reserve(ledger, run_id="https://example.test/?api_key=secret")


def usd_peak_prices(**provenance_overrides):
    provenance = {
        "billing_currency": "USD",
        "input_per_million": "0.3",
        "cached_input_per_million": "0.006",
        "output_per_million": "1.2",
        "cny_per_billing_unit": "8",
        "price_basis": "conservative_peak",
        "conversion_basis": "conservative_budget_rate",
        "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
    }
    return TokenPrices("2.4", "0.048", "9.6", provenance=provenance | provenance_overrides)


def test_usd_provenance_roundtrips_and_reports_estimates_without_claiming_invoice(ledger):
    prices = usd_peak_prices()
    assert TokenPrices(**prices.to_dict()) == prices
    assert isinstance(prices.provenance, PricingProvenance)
    assert prices.quote(input_tokens=12, output_tokens=1) == Decimal("0.0000384")
    assert prices.quote_billing_currency(input_tokens=12, output_tokens=1) == Decimal("0.0000048")
    reserve(ledger, prices=prices, input_tokens_upper_bound=12, max_output_tokens=1)
    ledger.mark_dispatched("attempt-1")
    result = ledger.settle("attempt-1", input_tokens=12, output_tokens=1, cached_input_tokens=0)
    assert result["estimated_cost_cny"] == "0.000038400"
    assert Decimal(result["estimated_cost_billing_currency"]) == Decimal("0.0000048")
    assert result["billing_currency"] == "USD"
    assert result["cost_basis"] == "configured_rate_estimate_not_invoice"
    assert "actual_cny" not in result
    assert result["prices"]["provenance"]["cny_per_billing_unit"] == "8"
    assert ledger.snapshot()["estimated_spent_cny"] == "0.000038400"


@pytest.mark.parametrize(
    "changed",
    [
        {"input_per_million": "0.4"},
        {"cached_input_per_million": "0.048"},
        {"output_per_million": "9.6"},
        {"cny_per_billing_unit": "7"},
        {"cny_per_billing_unit": 8.0},
        {"cny_per_billing_unit": "0"},
        {"billing_currency": "CNY"},
        {"billing_currency": "usd"},
        {"source_url": "https://example.test/prices?key=secret"},
        {"source_url": "https://secret@example.test/prices"},
    ],
)
def test_provenance_rejects_rate_mismatch_inexact_conversion_or_sensitive_url(changed):
    with pytest.raises(ValueError):
        usd_peak_prices(**changed)


def test_provenance_changes_conflict_with_existing_reservation(ledger):
    reserve(ledger, prices=usd_peak_prices())
    with pytest.raises(AttemptConflict):
        reserve(ledger, prices=usd_peak_prices(price_basis="changed_price_basis"))


def test_historical_probe_import_is_atomic_idempotent_and_not_a_new_http_dispatch(ledger):
    kwargs = {
        "attempt_id": "prior-ds-probe-12p-1c",
        "run_id": "preflight",
        "prices": usd_peak_prices(),
        "input_tokens": 12,
        "output_tokens": 1,
        "cached_input_tokens": 0,
        "evidence_id": "preflight-verified-probe",
    }
    first = ledger.import_prior_usage(**kwargs)
    second = ledger.import_prior_usage(**kwargs)
    assert first["created"] is True
    assert first | {"created": False} == second
    assert first["status"] == "settled"
    assert first["entry_kind"] == "historical_usage"
    assert first["dispatched_at"] is None
    assert ledger.snapshot()["estimated_spent_cny"] == "0.000038400"
    assert ledger.snapshot()["outstanding_reserved_cny"] == "0.000000000"
    assert [event["event"] for event in ledger.export_entries()[0]["events"]] == [
        "prior_usage_imported"
    ]
    for change in [{"input_tokens": 13}, {"evidence_id": "changed-evidence"}]:
        with pytest.raises(AttemptConflict):
            ledger.import_prior_usage(**(kwargs | change))
    with pytest.raises(AttemptConflict):
        ledger.mark_dispatched(kwargs["attempt_id"])


def test_historical_probe_reduces_budget_before_any_new_reservation(tmp_path):
    ledger = BudgetLedger(tmp_path / "prior.sqlite", "campaign", "0.00005")
    prices = usd_peak_prices()
    ledger.import_prior_usage(
        attempt_id="prior-probe",
        run_id="preflight",
        prices=prices,
        input_tokens=12,
        output_tokens=1,
        cached_input_tokens=0,
        evidence_id="prior-probe-proof",
    )
    with pytest.raises(BudgetExceeded):
        reserve(ledger, prices=prices, input_tokens_upper_bound=12, max_output_tokens=1)


def test_historical_usage_above_cap_is_recorded_then_blocks_pending_and_new_dispatch(ledger):
    reserve(ledger, "pending")
    imported = ledger.import_prior_usage(
        attempt_id="historic",
        run_id="preflight",
        prices=ONE_YUAN_PER_TOKEN,
        input_tokens=11,
        output_tokens=0,
        cached_input_tokens=0,
        evidence_id="authoritative-usage",
    )
    assert imported["estimated_cost_cny"] == "11.000000000"
    assert ledger.snapshot()["remaining_cny"] == "-2.000000000"
    assert ledger.snapshot()["blocked_reason"] == "historical_usage_exceeded_cap"
    with pytest.raises(BudgetExceeded):
        ledger.mark_dispatched("pending")
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "future")
