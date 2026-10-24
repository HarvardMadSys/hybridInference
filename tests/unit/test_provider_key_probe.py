"""How a failed provider-key probe is classified and reported to an admin.

The detail an admin sees quotes what the upstream answered, and an upstream may
echo the very key being verified; ``probe_error_detail`` must never return it.
Also covered: the reason codes the admin UI branches on, how the upstream's
message is read out of an error body, and which route is picked to probe with.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import aiohttp
import pytest

from serving.admin import provider_key_probe as probe

KEY = "sk-live-0123456789abcdefSECRET"


def _http_error(status: int, body: object = "") -> aiohttp.ClientResponseError:
    exc = aiohttp.ClientResponseError(request_info=None, history=(), status=status)  # type: ignore[arg-type]
    exc.error_body = body  # type: ignore[attr-defined]
    return exc


# --- the key never reaches the admin -----------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        _http_error(401, f'{{"error": {{"message": "invalid key {KEY}"}}}}'),
        _http_error(500, f"upstream exploded while using {KEY} ({KEY})".encode()),
        RuntimeError(f"connect failed for Authorization: Bearer {KEY}"),
    ],
    ids=["json-body", "bytes-body", "exception-text"],
)
def test_the_verified_key_is_redacted_from_every_detail(exc):
    detail = probe.probe_error_detail(exc, timeout_seconds=20, api_key=KEY)

    assert KEY not in detail
    assert "[redacted]" in detail


def test_a_long_upstream_body_is_cut_to_a_bounded_detail():
    detail = probe.probe_error_detail(_http_error(502, "x" * 5000), timeout_seconds=20)

    assert detail.startswith("Provider key verification failed with HTTP 502: ")
    assert detail.endswith("...")
    assert len(detail) <= len("Provider key verification failed with HTTP 502: ") + 503


def test_each_failure_kind_reads_as_what_happened():
    assert (
        probe.probe_error_detail(TimeoutError(), timeout_seconds=20)
        == "Provider key verification timed out after 20s"
    )
    assert (
        probe.probe_error_detail(_http_error(503), timeout_seconds=20)
        == "Provider key verification failed with HTTP 503"
    )
    assert (
        probe.probe_error_detail(ValueError(), timeout_seconds=20)
        == "Provider key verification failed: ValueError"
    )


# --- reason codes ------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (TimeoutError(), "timeout"),
        (_http_error(401), "auth_failed"),
        (_http_error(403), "auth_failed"),
        # A redirect is how some providers answer a bad key with their login page.
        (_http_error(302), "auth_failed"),
        (_http_error(500), "unexpected"),
        (_http_error(429), "unexpected"),
        (RuntimeError("boom"), "unexpected"),
        (
            _http_error(
                403, json.dumps({"error": {"message": probe.FEATHERLESS_PLAN_API_DISABLED_MESSAGE}})
            ),
            "plan_api_disabled",
        ),
    ],
    ids=["timeout", "401", "403", "302", "500", "429", "other", "plan-disabled"],
)
def test_reason_codes(exc, reason):
    assert probe.probe_error_reason(exc) == reason


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('{"error": {"message": "nested"}}', "nested"),
        (b'{"message": "top level bytes"}', "top level bytes"),
        ('{"detail": "as detail"}', "as detail"),
        ('{"error": "a string, not an object"}', ""),
        ('["not", "an", "object"]', ""),
        ("not json", ""),
        ("", ""),
    ],
)
def test_the_upstream_message_is_read_from_any_common_shape(body, message):
    assert probe._upstream_error_message(_http_error(400, body)) == message


# --- which route probes a key -------------------------------------------------


def _adapter(**config):
    defaults = {
        "id": "m",
        "provider": "acme",
        "base_url": "https://api.acme.test/v1",
        "model_type": "chat",
    }
    return SimpleNamespace(config=SimpleNamespace(**{**defaults, **config}))


def _services(*adapters):
    routes = {f"r{i}": SimpleNamespace(adapters=[(a, 1.0)]) for i, a in enumerate(adapters)}
    return SimpleNamespace(router=SimpleNamespace(routes=routes))


def test_only_a_usable_chat_route_of_that_provider_is_picked():
    usable = _adapter(id="chat-model")
    services = _services(
        _adapter(provider="other"),
        _adapter(model_type="embedding"),
        _adapter(base_url=None),
        _adapter(id=None),
        usable,
    )

    picked = probe.find_verification_adapter(services, "acme")

    assert picked is usable


def test_no_usable_route_means_nothing_to_probe_with():
    services = _services(_adapter(provider="other"), _adapter(model_type="embedding"))

    assert probe.find_verification_adapter(services, "acme") is None
    assert probe.find_verification_adapter(SimpleNamespace(), "acme") is None
