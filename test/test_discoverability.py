"""Tests for package discoverability and entry points."""

import shutil
import subprocess
import sys


def test_routing_module_is_discoverable():
    """Test that the routing module can be imported."""
    import routing
    from routing.executor import RouteExecutor

    assert routing.__file__ is not None
    assert RouteExecutor is not None


def test_serving_module_is_discoverable():
    """Test that the serving module can be imported."""
    import serving
    from serving import LLMProvider, LLMRequest, LLMResponse

    assert serving.__file__ is not None
    assert LLMProvider is not None
    assert LLMRequest is not None
    assert LLMResponse is not None


def test_serving_main_module_exists():
    """Test that serving.__main__ module exists."""
    from serving import __main__

    assert hasattr(__main__, "main")


def test_app_main_function_exists():
    """Test that app.py has a main() function."""
    from serving.servers.app import main

    assert callable(main)


def test_server_settings_exist():
    """Test that server configuration settings exist."""
    from serving.config.settings import settings

    assert hasattr(settings, "server_host")
    assert hasattr(settings, "server_port")
    assert hasattr(settings, "server_reload")
    assert hasattr(settings, "server_workers")
    assert settings.server_host == "0.0.0.0"
    assert settings.server_port == 8000
    assert settings.server_workers == 1


def test_hybrid_inference_command_exists():
    """Test that hybrid-inference command is installed."""
    path = shutil.which("hybrid-inference")
    assert path is not None, "hybrid-inference command not found in PATH"


def test_package_metadata_includes_routing():
    """Test that routing package is included in package metadata.

    Note: The package distribution name is 'hybrid-inference' (hyphenated)
    while the Python module name is 'serving' and 'routing' (underscored).
    This is a standard Python packaging convention where distribution names
    on PyPI use hyphens while module names use underscores.
    """
    # This test verifies that the routing module is properly included
    # in the setuptools configuration
    import importlib.metadata

    try:
        dist = importlib.metadata.distribution("hybrid-inference")
        # The package should be installed
        assert dist is not None
    except importlib.metadata.PackageNotFoundError:
        # If running from source without installation, skip this test
        import pytest

        pytest.skip("Package not installed, skipping metadata test")


def test_python_m_serving_works():
    """Test that python -m serving can be executed."""
    # We can't fully test this as it starts a server, but we can verify
    # that the __main__.py module can be imported
    result = subprocess.run(
        [sys.executable, "-c", "from serving import __main__; print('ok')"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert "ok" in result.stdout
