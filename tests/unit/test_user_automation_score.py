"""Unit tests for the pure scoring functions in ``user_automation_score``.

These exercise only the database-free helpers (client classification, hour-shape
statistics, and the per-user scorer), so they run in the default ``make test``
tier without a PostgreSQL connection.
"""

from __future__ import annotations

from typing import Any

import pytest

from ops.db.analysis.user_automation_score import (
    band_for,
    clamp01,
    classify_client,
    hour_shape,
    score_user,
    ua_automation_value,
    ua_base_from_breakdown,
)


def _stats(**overrides: Any) -> dict[str, Any]:
    """Build a per-user stats dict with neutral defaults, overriding as needed."""
    base: dict[str, Any] = {
        "n_req": 0,
        "n_chat": 0,
        "n_oneshot": 0,
        "p90_depth": None,
        "n_toolrows": 0,
        "n_toolpos": 0,
        "n_sz": 0,
        "p25_sz": None,
        "p50_sz": None,
        "p75_sz": None,
        "hour_hist": [0] * 24,
        "gap_p25": None,
        "gap_p50": None,
        "gap_p75": None,
        "n_gap": 0,
        "ua_base": None,
        "agent_share": 0.0,
    }
    base.update(overrides)
    return base


# --- client classification (parity with frontend parseClientTool) ------------


def test_classify_client_empty_or_missing() -> None:
    assert classify_client(None) is None
    assert classify_client("") is None
    assert classify_client("   ") is None


@pytest.mark.parametrize(
    ("ua", "expected"),
    [
        ("claude-code/0.1.0", "claude-code"),
        ("claude-cli/1.2.3", "claude-code"),
        ("Kilo-Code/1.2.3", "kilo-code"),
        ("cline/2.0.0", "cline"),
        ("codex-cli/0.5.0", "codex"),
        ("OpenAI/Python 1.40.0", "openai-python"),
        ("OpenAI/JS 4.104.0", "openai-node"),
        ("python-requests/2.32.5", "python-requests"),
        ("curl/8.0.1", "curl"),
        ("Go-http-client/1.1", "go-http"),
        ("Mozilla/5.0 (X11; Linux) Chrome/120", "browser"),
        ("myagent/1.0.0", "myagent"),
    ],
)
def test_classify_client_known_and_fallback(ua: str, expected: str) -> None:
    assert classify_client(ua) == expected


def test_ua_automation_value_by_class() -> None:
    assert ua_automation_value("claude-code/0.1.0") == pytest.approx(0.1)  # interactive
    assert ua_automation_value("Mozilla/5.0 Chrome/120") == pytest.approx(0.1)  # browser
    assert ua_automation_value("OpenAI/Python 1.0") == pytest.approx(0.5)  # ambiguous SDK
    assert ua_automation_value("curl/8.0") == pytest.approx(0.85)  # raw HTTP lib
    assert ua_automation_value("myagent/1.0") == pytest.approx(0.6)  # unknown token
    assert ua_automation_value(None) == pytest.approx(0.7)  # absent UA


def test_ua_base_is_request_weighted() -> None:
    assert ua_base_from_breakdown([]) is None
    # 90 curl (0.85) + 10 browser (0.1) -> request-weighted mean.
    base = ua_base_from_breakdown([("curl/8", 90), ("Mozilla/5.0 Chrome", 10)])
    assert base == pytest.approx((0.85 * 90 + 0.1 * 10) / 100)


# --- hour-shape statistics ---------------------------------------------------


def test_hour_shape_round_the_clock_is_automated() -> None:
    coverage, entropy_norm, max_gap = hour_shape([10] * 24)
    assert coverage == pytest.approx(1.0)
    assert entropy_norm == pytest.approx(1.0)
    assert max_gap == pytest.approx(0.0)


def test_hour_shape_daytime_block_has_rest_gap() -> None:
    hist = [0] * 24
    for hour in range(9, 18):  # active 09:00-17:00 only
        hist[hour] = 5
    coverage, _entropy, max_gap = hour_shape(hist)
    assert coverage == pytest.approx(9 / 24)
    # 18:00..08:00 inactive -> 15-hour quiet gap.
    assert max_gap == pytest.approx(15.0)


def test_hour_shape_single_active_hour() -> None:
    hist = [0] * 24
    hist[3] = 7
    _coverage, _entropy, max_gap = hour_shape(hist)
    assert max_gap == pytest.approx(23.0)


def test_hour_shape_empty() -> None:
    assert hour_shape([0] * 24) == (0.0, 0.0, 0.0)


# --- end-to-end scoring ------------------------------------------------------


def test_script_profile_scores_high() -> None:
    """One-shot, uniform-size, curl, 24/7, metronomic -> scripted_batch."""
    stats = _stats(
        n_req=500,
        n_chat=500,
        n_oneshot=500,
        p90_depth=1,
        n_toolrows=500,
        n_toolpos=0,
        n_sz=500,
        p25_sz=1000.0,
        p50_sz=1000.0,
        p75_sz=1000.0,  # zero dispersion
        hour_hist=[21] * 24,
        gap_p25=300.0,
        gap_p50=300.0,
        gap_p75=300.0,  # perfectly regular
        n_gap=499,
        ua_base=0.85,  # curl
        agent_share=0.0,
    )
    result = score_user(stats)
    assert result["score"] > 0.8
    assert result["band"] == "scripted_batch"
    assert result["insufficient_data"] is False
    assert result["confidence"] > 0.75


def test_human_profile_scores_low() -> None:
    """Multi-turn, varied sizes, browser, daytime-only, bursty -> likely_human."""
    hist = [0] * 24
    for hour in range(9, 18):
        hist[hour] = 20
    stats = _stats(
        n_req=200,
        n_chat=200,
        n_oneshot=40,  # f1 = 0.2
        p90_depth=8,  # deep threads -> depth dampening
        n_toolrows=200,
        n_toolpos=60,
        n_sz=200,
        p25_sz=200.0,
        p50_sz=600.0,
        p75_sz=2000.0,  # high dispersion
        hour_hist=hist,
        gap_p25=30.0,
        gap_p50=120.0,
        gap_p75=3600.0,  # bursty
        n_gap=199,
        ua_base=0.1,  # browser
        agent_share=0.0,
    )
    result = score_user(stats)
    assert result["score"] < 0.35
    assert result["band"] == "likely_human"


def test_no_tool_use_does_not_inflate_score() -> None:
    """A chat user with zero tool calls drops the tool signal (not scored as automated)."""
    stats = _stats(
        n_req=60,
        n_chat=60,
        n_oneshot=10,
        p90_depth=6,
        n_toolrows=60,
        n_toolpos=0,
        ua_base=0.1,
    )
    result = score_user(stats)
    assert result["signals"]["tool_call_human_tell"]["available"] is False


def test_unavailable_signals_are_dropped_not_zeroed() -> None:
    """A pure-embeddings batch user (no chat/turn data) is still scored on UA + activity."""
    stats = _stats(
        n_req=300,
        # No chat rows -> turn/size/tool signals must be dropped, not zeroed.
        n_chat=0,
        n_sz=0,
        hour_hist=[12] * 24,  # round-the-clock batch
        gap_p25=600.0,
        gap_p50=600.0,
        gap_p75=600.0,  # metronomic
        n_gap=299,
        ua_base=0.85,  # curl
        agent_share=0.0,
    )
    result = score_user(stats)
    sig = result["signals"]
    assert sig["turn_pattern"]["available"] is False
    assert sig["prompt_size_dispersion"]["available"] is False
    assert sig["tool_call_human_tell"]["available"] is False
    assert sig["client_tool_prior"]["available"] is True
    assert sig["daily_activity_shape"]["available"] is True
    # Curl UA + 24/7 metronomic activity, no human signals -> reads as automated.
    assert result["score"] > 0.6


def test_low_data_is_shrunk_toward_prior() -> None:
    """A 3-request user is flagged and pulled toward the neutral 0.5 prior."""
    stats = _stats(n_req=3, ua_base=0.85)
    result = score_user(stats)
    assert result["insufficient_data"] is True
    assert 0.45 < result["score"] < 0.6  # near the prior despite a scripty UA
    assert result["confidence"] < 0.1


def test_coding_agent_power_user_is_clamped() -> None:
    """High volume + coding-agent opener + a real nightly rest gap caps at mixed."""
    hist = [0] * 24
    for hour in range(8, 19):  # daytime, leaves a long nightly gap
        hist[hour] = 80
    stats = _stats(
        n_req=1000,
        n_chat=1000,
        n_oneshot=950,  # looks one-shot/scripty on turns...
        p90_depth=2,
        n_toolrows=1000,
        n_toolpos=0,
        n_sz=1000,
        p25_sz=5000.0,
        p50_sz=5000.0,
        p75_sz=5000.0,  # ...and uniform-size
        hour_hist=hist,
        gap_p25=10.0,
        gap_p50=10.0,
        gap_p75=10.0,  # regular
        ua_base=0.6,
        agent_share=0.9,  # but a coding-agent opener on nearly every request
        n_gap=999,
    )
    result = score_user(stats)
    assert result["score"] <= 0.5
    assert result["band"] in {"likely_human", "mixed_or_uncertain"}


def test_clamp_and_band_helpers() -> None:
    assert clamp01(-1.0) == 0.0
    assert clamp01(2.0) == 1.0
    assert clamp01(0.3) == pytest.approx(0.3)
    assert band_for(0.95) == "scripted_batch"
    assert band_for(0.7) == "likely_automated"
    assert band_for(0.5) == "mixed_or_uncertain"
    assert band_for(0.1) == "likely_human"
