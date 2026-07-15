"""Tests for relay process isolation."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from serving.oncall import security


def test_process_protection_is_linux_only(monkeypatch):
    monkeypatch.setattr(security.sys, "platform", "darwin")

    with patch.object(security.ctypes, "CDLL") as load_libc:
        security.protect_process_secrets()

    load_libc.assert_not_called()


def test_process_protection_disables_dumping_on_linux(monkeypatch):
    monkeypatch.setattr(security.sys, "platform", "linux")
    prctl = Mock(return_value=0)

    with patch.object(
        security.ctypes,
        "CDLL",
        return_value=SimpleNamespace(prctl=prctl),
    ):
        security.protect_process_secrets()

    prctl.assert_called_once_with(4, 0, 0, 0, 0)


def test_process_protection_fails_closed(monkeypatch):
    monkeypatch.setattr(security.sys, "platform", "linux")
    prctl = Mock(return_value=-1)

    with (
        patch.object(
            security.ctypes,
            "CDLL",
            return_value=SimpleNamespace(prctl=prctl),
        ),
        patch.object(security.ctypes, "get_errno", return_value=1),
        pytest.raises(OSError, match="Operation not permitted"),
    ):
        security.protect_process_secrets()
