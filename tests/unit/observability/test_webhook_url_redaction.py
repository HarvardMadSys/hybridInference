"""An alert webhook URL never reaches the log through an exception about a post.

The URL is the credential: whoever holds it can post to the channel. The
sender scrubs the URL from its own lines; these cover the lines rendered
further away, where an exception raised by a post is logged -- the
``alert_slack`` catch-all and the shutdown flush of settling recoveries. An
aiohttp ``ClientResponseError`` names its request URL in its repr, which is how
a webhook would leak.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from routing.routers import FixedRouter
from serving.observability.alerts import (
    AlertSeverity,
    SlackSink,
    alert_slack,
    redact_webhook_urls,
    reset_dedupe_state,
)
from serving.servers import bootstrap
from serving.servers.deps import AppServices
from serving.utils.secret_urls import url_fingerprint

SLACK = "https://hooks.slack.com/services/T0SECRET/B0SECRET/xoxbsecretpath"
WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-secret-key"


@pytest.fixture(autouse=True)
def _no_inherited_webhooks(monkeypatch):
    for variable in ("SLACK_ALERTS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "WECOM_ALERTS_WEBHOOK_URL"):
        monkeypatch.delenv(variable, raising=False)
    reset_dedupe_state()
    yield
    reset_dedupe_state()


def _response_error(url: str) -> aiohttp.ClientResponseError:
    """What an aiohttp post to ``url`` raises for a 404, URL and all."""
    info = aiohttp.RequestInfo(
        url=URL(url), method="POST", headers=CIMultiDictProxy(CIMultiDict()), real_url=URL(url)
    )
    return aiohttp.ClientResponseError(info, (), status=404, message="Not Found")


@pytest.mark.parametrize(
    ("variable", "url", "secret"),
    [
        ("SLACK_ALERTS_WEBHOOK_URL", SLACK, "T0SECRET/B0SECRET"),
        ("SLACK_WEBHOOK_URL", SLACK, "xoxbsecretpath"),
        ("WECOM_ALERTS_WEBHOOK_URL", WECOM, "wecom-secret-key"),
    ],
)
def test_every_configured_webhook_is_scrubbed_from_an_exception_repr(
    monkeypatch, variable, url, secret
):
    monkeypatch.setenv(variable, url)
    text = repr(_response_error(url))
    assert secret in text  # what would have been logged

    redacted = redact_webhook_urls(text)

    assert secret not in redacted
    assert url_fingerprint(url) in redacted  # still says which sink
    assert "404" in redacted


def test_text_without_a_webhook_is_left_alone(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", SLACK)
    assert redact_webhook_urls("TimeoutError()") == "TimeoutError()"


async def test_the_shutdown_flush_logs_no_webhook(monkeypatch, caplog):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", SLACK)

    async def flush_raising() -> None:
        raise _response_error(SLACK)

    monkeypatch.setattr(bootstrap, "flush_settling", flush_raising)
    with caplog.at_level(logging.WARNING, logger=bootstrap.logger.name):
        await bootstrap.shutdown(AppServices(router=FixedRouter()))

    [line] = [
        r.getMessage()
        for r in caplog.records
        if "Settling circuit recoveries were not all announced" in r.getMessage()
    ]
    assert "ClientResponseError" in line
    assert "T0SECRET" not in line
    assert "xoxbsecretpath" not in line


async def test_a_sink_that_raises_quoting_its_webhook_is_logged_scrubbed(monkeypatch, caplog):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", SLACK)

    async def post_raising(self, message, *, dedupe_key):
        raise _response_error(self.webhook_url)

    # ``post`` without alert history installed, ``post_detailed`` with it.
    with (
        patch.object(SlackSink, "post", post_raising),
        patch.object(SlackSink, "post_detailed", post_raising),
        caplog.at_level(logging.ERROR, logger="serving.observability.alerts"),
    ):
        sent = await alert_slack(AlertSeverity.ERROR, "test", {"k": "v"}, dedupe_key="leak")

    assert sent is False
    [line] = [r.getMessage() for r in caplog.records if "alert_slack post raised" in r.getMessage()]
    assert "ClientResponseError" in line
    assert "T0SECRET" not in line
    assert "xoxbsecretpath" not in line
