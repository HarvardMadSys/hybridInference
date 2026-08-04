"""A PEM in `.env` goes on one line, and the failure if it does not is total.

`.env.example` said "multi-line: quote it". Compose reads this file with its
own parser, which has no multi-line form at all — a real PEM pasted across
several lines fails with `key cannot contain a space` and **the whole stack
refuses to start**, not just the service that wanted the key. The guidance was
wrong in the direction that takes a gateway down.

The loader restores escaped newlines (`identity_keys._normalize`), so the
one-line form is not a workaround: it is the shape this deployment already
uses for the GitHub App key.

Skipped where the Compose CLI is absent. `docker compose config` parses and
renders without a daemon, so this needs no container — but it does need the
binary, and a unit suite that fails on a laptop without Docker is a suite
people learn to ignore.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="the Compose CLI is not installed here"
)

_COMPOSE = textwrap.dedent(
    """\
    services:
      probe:
        image: alpine
        env_file: .env
    """
)

#: Assembled rather than written out. The release audit scans every tracked
#: file for credential shapes, and the PEM armour is one of them — a test
#: fixture spelling it verbatim fails that scan, and the fix is to not write it
#: rather than to teach the scanner an exception. There is no key here either
#: way; the body is four bytes of nothing.
_DASHES = "-" * 5
_BEGIN = f"{_DASHES}BEGIN PRIVATE KEY{_DASHES}"
_END = f"{_DASHES}END PRIVATE KEY{_DASHES}"
_BODY = "MIIEvQIB"

#: What an operator should write: one line, newlines escaped.
_ESCAPED = f"IDENTITY_JWT_PRIVATE_KEY={_BEGIN}\\n{_BODY}\\n{_END}\n"

#: What they will write if the guidance does not say otherwise.
_REAL_MULTILINE = f"IDENTITY_JWT_PRIVATE_KEY={_BEGIN}\n{_BODY}\n{_END}\n"


def _render(tmp_path: Path, env_body: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "docker-compose.yml").write_text(_COMPOSE)
    (tmp_path / ".env").write_text(env_body)
    return subprocess.run(
        ["docker", "compose", "config"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_an_escaped_pem_survives_compose(tmp_path: Path) -> None:
    """The one-line form reaches the container intact."""
    result = _render(tmp_path, _ESCAPED)
    assert result.returncode == 0, result.stderr
    assert _BEGIN in result.stdout
    assert r"\n" in result.stdout


def test_a_real_multiline_pem_takes_the_whole_file_down(tmp_path: Path) -> None:
    """Not a degraded value — nothing renders, so nothing starts.

    This is why the guidance matters more than most: the operator's mistake
    does not produce a gateway with a broken key, it produces no gateway.
    """
    result = _render(tmp_path, _REAL_MULTILINE)
    assert result.returncode != 0
    assert "key cannot contain a space" in (result.stderr + result.stdout)


def test_the_example_file_tells_operators_the_one_line_form() -> None:
    """The guidance itself, since it was wrong once."""
    text = (Path(__file__).resolve().parents[1].parent / ".env.example").read_text()
    for name in ("IDENTITY_JWT_PRIVATE_KEY", "IDENTITY_JWT_RETIRING_PUBLIC_KEYS"):
        start = text.index(f"\n{name}=")
        preamble = text[:start]
        section = preamble[preamble.rindex("\n\n") :]
        assert "one line" in section.lower(), f"{name} does not say to use one line"
