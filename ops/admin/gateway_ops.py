"""Gateway diagnostics, pinned-artifact deployment and isolated recovery drills.

Compose arguments come from the Makefile as an argv list, never a shell command.
Only deploy and rollback recreate application services. Each deployment keeps
its record -- the images it is pinned to and the releases it ran -- in a state
directory of its own, which the Makefile names after the selected distribution.
A restore drill runs in a throwaway PostgreSQL server with its own container,
volume and network, never in the deployment's database server.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, NoReturn

_DIGEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")

#: The overlay that starts the application services from pinned images.
_ARTIFACTS = "deploy/docker/docker-compose.artifacts.yml"
#: The overlay that defines the restore drill's throwaway database server.
_RESTORE_OVERLAY = "deploy/docker/docker-compose.restore-check.yml"
_RESTORE_SERVICE = "restore-check"
_GIB = 1 << 30

# What the operations themselves raise and report; anything else is a bug.
_FAILURES = (OSError, ValueError, subprocess.CalledProcessError, http.client.HTTPException)


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
    check_health(base_url)
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


def check_health(base_url: str) -> dict[str, Any]:
    """Require healthy stores through the public health path forwarded by the console."""
    health = request(base_url, "/health")
    if health.get("status") != "healthy":
        raise ValueError("Gateway health is not healthy")
    return health


def describe(exc: BaseException) -> str:
    """Name a failure for the operator without the output of what failed.

    Command lines carry file paths and image references, never a secret: values
    reach Compose through the environment. HTTP and parsing errors name a
    status or a position, not a body.
    """
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    if isinstance(exc, subprocess.CalledProcessError):
        return f"`{shlex.join(str(arg) for arg in exc.cmd)}` exited with status {exc.returncode}"
    text = str(exc).strip()
    if type(exc) is ValueError:
        return text or "ValueError"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _records(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text or "[]")
        return data if isinstance(data, list) else [data]
    except ValueError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def _compose_config(compose: list[str]) -> dict[str, Any]:
    """Render the project with the same overlays and env files as every lifecycle command."""
    return json.loads(
        command([*compose, "config", "--format", "json"], capture=True, text=True).stdout
    )


def published_base_url(compose: list[str], service: str) -> str:
    """Resolve the probe endpoint with the same overlays and env files as deployment."""
    config = _compose_config(compose)
    target = {"backend": 8080, "frontend": 3001}[service]
    for port in config.get("services", {}).get(service, {}).get("ports", []):
        if port.get("target") != target or port.get("protocol", "tcp") != "tcp":
            continue
        published = str(port.get("published", ""))
        if not published.isdigit() or not 1 <= int(published) <= 65535:
            continue
        host = port.get("host_ip") or "localhost"
        if host in ("0.0.0.0", "::"):
            host = "localhost"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{published}"
    raise ValueError(f"No published {service} endpoint; specify --base-url explicitly")


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
        ("readiness", lambda: check_health(args.base_url).get("status")),
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
        except Exception as exc:  # one failed check, a short read included, hides no other
            failed = True
            results[name] = {"error": type(exc).__name__}
    print(json.dumps(results, indent=2))
    site = results.get("site", {})
    incomplete = site.get("setup", {}).get("required") or site.get("configuration", {}).get(
        "incomplete"
    )
    return int(failed or bool(incomplete))


def _write_private(path: Path, text: str) -> None:
    """Replace *path* atomically with a file only its owner can read."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        temp.unlink()
    with open(temp, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        stream.write(text)
    temp.replace(path)


def _write_json(path: Path, value: Any) -> None:
    _write_private(path, json.dumps(value, indent=2))


def _pin(state: Path, images: dict[str, str]) -> None:
    """Record the images this deployment's later lifecycle commands start.

    The Makefile hands ``images.env`` to Compose, with the pinned-artifact
    overlay, for this deployment alone.
    """
    _write_private(
        state / "images.env",
        "".join(f"{service.upper()}_IMAGE={images[service]}\n" for service in sorted(images)),
    )


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
    return compose if _ARTIFACTS in compose else [*compose, "-f", _ARTIFACTS]


def _image_environment(images: dict[str, str]) -> dict[str, str]:
    # The shell outranks every --env-file, including a pin recorded earlier.
    return {
        **os.environ,
        **{f"{service.upper()}_IMAGE": image for service, image in images.items()},
    }


def _switch(compose: list[str], images: dict[str, str]) -> None:
    """Start the application services from *images*, without building or migrating."""
    command(
        [*_artifact_command(compose), "up", "-d", "--no-deps", "--no-build", "backend", "frontend"],
        env=_image_environment(images),
    )


def _wait(args: argparse.Namespace) -> None:
    """Repeat the acceptance smoke until it passes; on timeout, report its last failure."""
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            smoke(args.base_url, args.model)
            return
        except Exception as exc:  # any failed probe -- a reset, a short read -- is "not yet"
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"acceptance did not pass within {args.timeout:g}s; last check: {describe(exc)}"
                ) from exc
            time.sleep(2)


def _restore_previous(
    args: argparse.Namespace, previous: dict[str, str], failure: BaseException
) -> NoReturn:
    """Start the images that ran before the deploy again, accept them, and report why."""
    reason = describe(failure)
    print(f"Deployment not accepted ({reason}); restoring the previous images", file=sys.stderr)
    try:
        _switch(args.compose, previous)
        _wait(args)
    except BaseException as restore_failure:
        raise ValueError(
            f"deployment not accepted: {reason}. Restoring the previous images failed too: "
            f"{describe(restore_failure)}"
        ) from restore_failure
    raise ValueError(
        f"deployment not accepted: {reason}. The previous images are running again and "
        "pass acceptance"
    ) from failure


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
    env = _image_environment(images)
    compose = _artifact_command(args.compose)
    command([*compose, "pull", "backend", "frontend"], env=env)
    command([*args.compose, "up", "-d", "--no-recreate", "--wait", "postgres"])
    command(
        [*compose, "run", "--rm", "--no-deps", "backend", "python", "-m", "migrations", "upgrade"],
        env=env,
    )
    if args.legacy_manifest:
        # Before the switch, with the new image, so the release starts on the
        # site policy and alert rules its predecessor read from files. Settings
        # already stored are kept, so running it again changes nothing.
        command(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "backend",
                "python",
                "-m",
                "serving.config.manage",
                "import-legacy",
                *args.legacy_import_option,
                args.legacy_manifest,
            ],
            env=env,
        )
    try:
        _switch(args.compose, images)
        _wait(args)
    except BaseException as failure:  # Ctrl-C and a short read too: never leave it half-switched
        _restore_previous(args, previous, failure)
    # Only an accepted release becomes the one later commands start, and the
    # one before it what a rollback returns to; a failed attempt changes neither.
    _write_json(args.state / "previous.json", previous)
    _write_json(args.state / "current.json", images)
    _pin(args.state, images)
    print("Deployment accepted")


def rollback(args: argparse.Namespace) -> None:
    """Restore recorded immutable artifacts; never run old-image migrations."""
    record = args.state / "previous.json"
    if not record.exists():
        raise ValueError(
            f"No release to return to is recorded in {args.state}; a deploy records one once "
            "it is accepted"
        )
    previous = json.loads(record.read_text())
    if set(previous) != {"backend", "frontend"} or not all(
        _IMAGE_ID.fullmatch(value) or _DIGEST.fullmatch(value) for value in previous.values()
    ):
        raise ValueError("Recorded rollback artifacts are invalid")
    _switch(args.compose, previous)
    # Pinned as soon as they run, accepted or not: a later `make up` must keep
    # what the operator rolled back to, never start the release they left.
    _pin(args.state, previous)
    _wait(args)
    _write_json(args.state / "current.json", previous)
    print("Rollback accepted")


def backup(args: argparse.Namespace) -> None:
    """Create a private database dump without a TTY or secret command arguments."""
    args.backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    print(f"Database backup completed: {args.backup}")


def _database_bytes(compose: list[str]) -> int | None:
    """Return the deployment database's size, or None when its server is not running."""
    try:
        output = command(
            [
                *compose,
                "exec",
                "-T",
                "postgres",
                "sh",
                "-c",
                'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc '
                '"SELECT pg_database_size(current_database())"',
            ],
            capture=True,
            text=True,
        ).stdout
        return int(output.strip())
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def _free_bytes(compose: list[str], env: dict[str, str]) -> int:
    """Return the free space on the filesystem that holds the scratch server's volume."""
    output = command(
        [*compose, "exec", "-T", _RESTORE_SERVICE, "df", "-Pk", "/var/lib/postgresql/data"],
        capture=True,
        text=True,
        env=env,
    ).stdout
    return int(output.strip().splitlines()[-1].split()[3]) * 1024


def _remove_scratch(name: str) -> None:
    """Remove the drill's container, volume and network, and name any that stayed."""
    left = []
    for kind in ("container", "volume", "network"):
        removal = ["docker", kind, "rm", *(["--force"] if kind == "container" else []), name]
        with contextlib.suppress(OSError, subprocess.CalledProcessError):
            command(removal, capture=True)
        try:
            command(["docker", kind, "inspect", name], capture=True)
        except (OSError, subprocess.CalledProcessError):
            continue
        left.append(kind)
    if left:
        print(
            f"restore-check could not remove its {' and '.join(left)} {name}; the volume holds "
            f"a copy of the backup. Remove them with `docker <kind> rm {name}`.",
            file=sys.stderr,
        )


def restore_check(args: argparse.Namespace) -> None:
    """Restore a backup into a throwaway server and migrate it to this release's head.

    The scratch server runs the deployment's PostgreSQL image with its own
    container, volume and internal network, so the restore adds no load, WAL
    or data to the server the gateway uses. Its volume still lives where Docker
    keeps volumes, which is normally the disk of the deployment's own, so the
    drill first checks there is room: twice the deployment database's size (the
    dump's, when that server is not running) plus 1 GiB.
    """
    dump_bytes = args.backup.stat().st_size  # a missing dump fails before anything starts
    image = _compose_config(args.compose)["services"]["postgres"]["image"]
    name = "gateway-restore-" + uuid.uuid4().hex[:16]
    compose = [*args.compose, "-f", _RESTORE_OVERLAY]
    env = {**os.environ, "RESTORE_CHECK_NAME": name, "RESTORE_CHECK_IMAGE": image}
    needed = 2 * max(_database_bytes(args.compose) or 0, dump_bytes) + _GIB
    try:
        command([*compose, "up", "-d", "--no-deps", "--wait", _RESTORE_SERVICE], env=env)
        free = _free_bytes(compose, env)
        if free < needed:
            raise ValueError(
                f"The restore check needs about {needed / _GIB:.1f} GiB free where Docker keeps "
                f"volumes and {free / _GIB:.1f} GiB is free; free some space or run the check "
                "on another host"
            )
        with args.backup.open("rb") as stream:
            command(
                [
                    *compose,
                    "exec",
                    "-T",
                    _RESTORE_SERVICE,
                    "sh",
                    "-c",
                    # Owners and grants name roles of the deployment's server
                    # that a fresh one lacks; the gateway's own user owns all.
                    'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
                    "--no-owner --no-privileges --exit-on-error",
                ],
                stdin=stream,
                env=env,
            )
        # The release's own migrations, as a deploy runs them: a backup taken
        # before an upgrade is behind, and restoring it means migrating it.
        migrations = [
            *compose,
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"DB_HOST={_RESTORE_SERVICE}",
            "backend",
            "python",
            "-m",
            "migrations",
        ]
        command([*migrations, "upgrade"], env=env)
        command([*migrations, "status"], env=env)
    finally:
        _remove_scratch(name)
    print("Isolated restore check passed: the backup restores and migrates to this release")


def main() -> int:
    """Run the requested operation, with concise failures that contain no secrets."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("doctor", "smoke", "deploy", "rollback", "backup", "restore-check")
    )
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--backend-image", default="")
    parser.add_argument("--frontend-image", default="")
    parser.add_argument(
        "--legacy-manifest",
        default="",
        help="deploy: import this previous-release manifest, at its path inside the backend "
        "container, with the new image before switching",
    )
    parser.add_argument(
        "--legacy-import-option",
        action="append",
        default=[],
        help="deploy: pass an option to import-legacy; repeat for several",
    )
    parser.add_argument("--backup", type=Path, default=Path(".ops/none/gateway.dump"))
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(".ops/none"),
        help="this deployment's record: its pinned images and release history",
    )
    parser.add_argument("--compose", nargs=argparse.REMAINDER, default=["docker", "compose"])
    args = parser.parse_args()
    try:
        if args.action in ("doctor", "smoke", "deploy", "rollback") and not args.base_url:
            args.base_url = published_base_url(
                args.compose, "backend" if args.action == "smoke" else "frontend"
            )
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
    except _FAILURES as exc:
        print(f"{args.action} failed: {describe(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"{args.action} interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
