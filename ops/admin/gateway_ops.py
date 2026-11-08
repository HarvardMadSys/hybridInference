"""Gateway diagnostics, pinned-artifact deployment and isolated recovery drills.

Compose arguments come from the Makefile as an argv list, never a shell command.
Only deploy and rollback recreate application services. A restore drill creates
and drops its own randomly named database, never the deployment's database.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

_DIGEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")


def command(
    argv: list[str], *, capture: bool = False, **kwargs: Any
) -> subprocess.CompletedProcess:
    """Run a fixed argv command; failures abort the operation."""
    return subprocess.run(argv, check=True, capture_output=capture, **kwargs)


def request(
    base_url: str, path: str, payload: dict[str, Any] | None = None, *, token: str | None = None
) -> Any:
    """Read an API response without printing response bodies or credentials."""
    headers = {"Content-Type": "application/json"}
    key = token or os.environ.get("GATEWAY_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if payload is not None:
        headers["X-Probe"] = "synthetic"
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def smoke(base_url: str, model: str = "") -> dict[str, Any]:
    """Check the public request path, including inference when a model is supplied."""
    request(base_url, "/health/ready")
    site = request(base_url, "/site-config")
    if site.get("setup", {}).get("required") or site.get("configuration", {}).get("incomplete"):
        raise ValueError("Ask admin to configure this service")
    models = request(base_url, "/v1/models")
    if model:
        completion = request(
            base_url,
            "/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply OK"}],
                "max_tokens": 4,
            },
        )
        if not completion.get("choices"):
            raise ValueError("Inference smoke did not return a completion")
    return {
        "ready": True,
        "models_visible": len(models.get("data", [])),
        "inference_checked": bool(model),
    }


def _records(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text or "[]")
        return data if isinstance(data, list) else [data]
    except ValueError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def configuration_status(args: argparse.Namespace) -> dict[str, Any]:
    """Read applied status from the running process when administrator access is supplied."""
    token = os.environ.get("ADMIN_ACCESS_TOKEN")
    if token:
        body = request(args.base_url, "/admin/config", token=token)
        return {
            "view": "running process",
            "version": body.get("source_version"),
            "missing": body.get("missing", []),
            "pending_restart": body.get("pending_restart", []),
            "invalid": [item["key"] for item in body["entries"] if item.get("invalid")],
        }
    body = json.loads(
        command(
            [
                *args.compose,
                "exec",
                "-T",
                "backend",
                "python",
                "-m",
                "serving.config.manage",
                "status",
            ],
            capture=True,
            text=True,
        ).stdout
    )
    # A recovery CLI starts a fresh process: it cannot know which saved values
    # the serving process has not applied yet. Never report that as no restarts.
    body["pending_restart"] = "not checked; set ADMIN_ACCESS_TOKEN for running-process status"
    body["view"] = "saved configuration"
    return body


def doctor(args: argparse.Namespace) -> int:
    """Collect independent diagnostic results, even when one check fails."""
    results: dict[str, Any] = {}
    failed = False
    for name, action in (
        (
            "services",
            lambda: _records(
                command([*args.compose, "ps", "--format", "json"], capture=True, text=True).stdout
                or "[]"
            ),
        ),
        ("readiness", lambda: request(args.base_url, "/health/ready").get("status")),
        (
            "site",
            lambda: {
                k: v
                for k, v in request(args.base_url, "/site-config").items()
                if k in ("setup", "configuration")
            },
        ),
        (
            "schema",
            lambda: command(
                [*args.compose, "exec", "-T", "backend", "python", "-m", "migrations", "status"],
                capture=True,
                text=True,
            ).stdout.strip(),
        ),
        ("configuration", lambda: configuration_status(args)),
    ):
        try:
            result = action()
            if name == "services":
                # Docker reports command lines and host paths too; diagnostics need neither.
                result = [
                    {k: row.get(k) for k in ("Service", "State", "Health", "Image")}
                    for row in (result if isinstance(result, list) else [result])
                ]
            if name == "services" and any(
                row.get("State") != "running" or row.get("Health") == "unhealthy" for row in result
            ):
                failed = True
            if name == "configuration" and (result.get("missing") or result.get("invalid")):
                failed = True
            results[name] = result
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            failed = True
            results[name] = {"error": type(exc).__name__}
    print(json.dumps(results, indent=2))
    site = results.get("site", {})
    incomplete = site.get("setup", {}).get("required") or site.get("configuration", {}).get(
        "incomplete"
    )
    return int(failed or bool(incomplete))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(value, stream, indent=2)
    temp.replace(path)


def _running_images(compose: list[str]) -> dict[str, str]:
    images = {}
    for service in ("backend", "frontend"):
        container = command([*compose, "ps", "-q", service], capture=True, text=True).stdout.strip()
        if container:
            images[service] = command(
                ["docker", "inspect", "--format", "{{.Image}}", container], capture=True, text=True
            ).stdout.strip()
    return images


def _artifact_command(compose: list[str]) -> list[str]:
    # Options precede the Compose subcommand; its relative paths share the base
    # file's directory, as in every other lifecycle command.
    path = "deploy/docker/docker-compose.artifacts.yml"
    return compose if path in compose else [*compose, "-f", path]


def _switch(compose: list[str], images: dict[str, str]) -> None:
    env = {**os.environ, **{f"{service.upper()}_IMAGE": image for service, image in images.items()}}
    state = Path(".ops")
    state.mkdir(mode=0o700, exist_ok=True)
    selection = state / "images.env"
    temp = state / "images.env.tmp"
    temp.write_text(
        "".join(f"{service.upper()}_IMAGE={image}\n" for service, image in images.items())
    )
    temp.replace(selection)
    command(
        [*_artifact_command(compose), "up", "-d", "--no-deps", "--no-build", "backend", "frontend"],
        env=env,
    )


def _wait(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            smoke(args.base_url, args.model)
            return
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise ValueError("Deployment acceptance timed out") from None
            time.sleep(2)


def deploy(args: argparse.Namespace) -> None:
    """Migrate with pinned artifacts, accept the new service, or restore the old artifacts."""
    images = {"backend": args.backend_image, "frontend": args.frontend_image}
    if not all(_DIGEST.fullmatch(image) for image in images.values()):
        raise ValueError("Specify backend and frontend images as image@sha256:<64 hex digits>")
    previous = _running_images(args.compose)
    if set(previous) != {"backend", "frontend"}:
        raise ValueError(
            "Start the stack before upgrading; both running artifacts must be recorded"
        )
    env = {**os.environ, "BACKEND_IMAGE": images["backend"], "FRONTEND_IMAGE": images["frontend"]}
    compose = _artifact_command(args.compose)
    command([*compose, "pull", "backend", "frontend"], env=env)
    command([*args.compose, "up", "-d", "--no-recreate", "--wait", "postgres"])
    command(
        [*compose, "run", "--rm", "--no-deps", "backend", "python", "-m", "migrations", "upgrade"],
        env=env,
    )
    _write_json(args.state / "previous.json", previous)
    try:
        _switch(args.compose, images)
        _wait(args)
    except (OSError, ValueError, subprocess.CalledProcessError):
        _switch(args.compose, previous)
        raise
    _write_json(args.state / "current.json", images)
    print("Deployment accepted")


def rollback(args: argparse.Namespace) -> None:
    """Restore recorded immutable artifacts; never run old-image migrations."""
    previous = json.loads((args.state / "previous.json").read_text())
    if set(previous) != {"backend", "frontend"} or not all(
        _IMAGE_ID.fullmatch(value) or _DIGEST.fullmatch(value) for value in previous.values()
    ):
        raise ValueError("Recorded rollback artifacts are invalid")
    _switch(args.compose, previous)
    _wait(args)
    _write_json(args.state / "current.json", previous)
    print("Rollback accepted")


def backup(args: argparse.Namespace) -> None:
    """Create a private database dump without a TTY or secret command arguments."""
    with open(args.backup, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        try:
            command(
                [
                    *args.compose,
                    "exec",
                    "-T",
                    "postgres",
                    "sh",
                    "-c",
                    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc',
                ],
                stdout=stream,
            )
        except BaseException:
            stream.close()
            with contextlib.suppress(OSError):
                args.backup.unlink()
            raise
    print("Database backup completed")


def restore_check(args: argparse.Namespace) -> None:
    """Restore into a unique scratch database and check schema compatibility."""
    name = "gateway_restore_" + uuid.uuid4().hex
    prefix = [*args.compose, "exec", "-T", "postgres", "sh", "-c"]
    command([*prefix, 'exec createdb -U "$POSTGRES_USER" "$1"', "restore-check", name])
    try:
        with args.backup.open("rb") as stream:
            command(
                [
                    *prefix,
                    'exec pg_restore -U "$POSTGRES_USER" -d "$1" --exit-on-error',
                    "restore-check",
                    name,
                ],
                stdin=stream,
            )
        command(
            [
                *args.compose,
                "run",
                "--rm",
                "--no-deps",
                "-e",
                f"DB_NAME={name}",
                "backend",
                "python",
                "-m",
                "migrations",
                "status",
            ]
        )
    finally:
        command([*prefix, 'exec dropdb -U "$POSTGRES_USER" "$1"', "restore-check", name])
    print("Isolated restore check passed")


def main() -> int:
    """Run the requested operation, with concise failures that contain no secrets."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("doctor", "smoke", "deploy", "rollback", "backup", "restore-check")
    )
    parser.add_argument("--base-url", default="http://localhost:3001")
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--backend-image", default="")
    parser.add_argument("--frontend-image", default="")
    parser.add_argument("--backup", type=Path, default=Path("gateway.dump"))
    parser.add_argument("--state", type=Path, default=Path(".ops/releases"))
    parser.add_argument("--compose", nargs=argparse.REMAINDER, default=["docker", "compose"])
    args = parser.parse_args()
    try:
        if args.action == "doctor":
            return doctor(args)
        if args.action == "smoke":
            print(json.dumps(smoke(args.base_url, args.model)))
        else:
            {
                "deploy": deploy,
                "rollback": rollback,
                "backup": backup,
                "restore-check": restore_check,
            }[args.action](args)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"{args.action} failed: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
