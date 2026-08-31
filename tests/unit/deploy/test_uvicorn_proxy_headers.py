"""Every uvicorn launch must opt out of uvicorn's own proxy-header handling.

``serving/utils/request_ip.py`` is documented as the single place that decides
whether forwarded headers are believed, gated by ``TRUST_PROXY_HEADERS``
(default off). uvicorn has a second, independent implementation of the same
idea — and it defaults ON: unless told otherwise it rewrites
``request.client`` and the URL scheme from ``X-Forwarded-For`` /
``X-Forwarded-Proto`` for any peer in ``--forwarded-allow-ips`` (default
``127.0.0.1``, or the ``FORWARDED_ALLOW_IPS`` env var). That rewrite happens
*before* any application code runs, so the application's trust gate would be
filtering an already-spoofed "socket peer".

The Docker image shipped with the worst form of this for a while:
``--proxy-headers --forwarded-allow-ips "*"`` let any caller rewrite their own
address with a bare header, which defeated the per-IP signup/login rate limits
and made ``api_logs`` addresses caller-controlled while ``TRUST_PROXY_HEADERS``
stayed 0 (HarvardMadSys/freeInference#72).

Deleting the flags is not enough — the default is ON — so every launch site
must carry ``--no-proxy-headers`` explicitly. These tests parse the deploy
files directly; no Docker or systemd is required. A new uvicorn launch site
added under ``deploy/`` is picked up by the scan automatically.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "deploy"

# Launch sites this repository is known to ship. The scan below finds these
# and any future ones; this list only guards against the scan silently going
# blind (e.g. a refactor that moves the CMD into a script the scan can't see).
_KNOWN_LAUNCH_FILES = {
    DEPLOY / "docker" / "Dockerfile.backend",
    DEPLOY / "docker" / "Dockerfile.oncall",
    DEPLOY / "systemd" / "hybrid_inference.service",
    DEPLOY / "systemd" / "hybrid_inference.staging.service",
}


def _uvicorn_launch_lines(path: Path) -> list[str]:
    """Return each uvicorn app launch in *path* as one logical line.

    Dockerfile CMDs and systemd ExecStart values both spread one command over
    several physical lines with ``\\`` continuations; folding those first means
    a flag is found no matter which physical line it sits on.
    """
    text = path.read_text(encoding="utf-8", errors="ignore")
    folded = text.replace("\\\n", " ")
    return [
        line
        for line in folded.splitlines()
        if "uvicorn" in line and ":app" in line and not line.lstrip().startswith("#")
    ]


def _scan_deploy_tree() -> dict[Path, list[str]]:
    launches: dict[Path, list[str]] = {}
    for path in sorted(DEPLOY.rglob("*")):
        if not path.is_file():
            continue
        lines = _uvicorn_launch_lines(path)
        if lines:
            launches[path] = lines
    return launches


def test_scan_still_sees_the_known_launch_sites() -> None:
    """If this fails, the scan went blind — fix the scan, don't shrink the set."""
    found = set(_scan_deploy_tree())
    missing = _KNOWN_LAUNCH_FILES - found
    assert not missing, f"uvicorn launches no longer detected in: {sorted(missing)}"


def test_every_uvicorn_launch_disables_proxy_headers_explicitly() -> None:
    for path, lines in _scan_deploy_tree().items():
        for line in lines:
            rel = path.relative_to(REPO)
            assert "--forwarded-allow-ips" not in line, (
                f"{rel}: --forwarded-allow-ips hands uvicorn a trust decision that "
                f"belongs to TRUST_PROXY_HEADERS in request_ip.py: {line.strip()}"
            )
            # Substring check is safe: "--no-proxy-headers" does not contain
            # the double-dash form "--proxy-headers".
            assert "--proxy-headers" not in line, (
                f"{rel}: uvicorn must not interpret forwarded headers; "
                f"drop --proxy-headers: {line.strip()}"
            )
            assert "--no-proxy-headers" in line, (
                f"{rel}: uvicorn's proxy-header handling defaults ON (trusting "
                f"127.0.0.1 / $FORWARDED_ALLOW_IPS); it must be switched off "
                f"explicitly with --no-proxy-headers: {line.strip()}"
            )
