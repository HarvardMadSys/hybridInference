"""Black-box acceptance check for the full local example stack.

Every HTTP request uses the frontend origin. The caller supplies the exact
Compose command used to recreate backend; credentials remain in this process's
memory during the ordinary smoke. CI may explicitly request a private,
outside-checkout reset-proof file so a later process can prove destructive
reset invalidates the old password and API key.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import os
import stat
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

ADMIN_EMAIL = "admin@local.dev"
DEFAULT_ADMIN_PASSWORD = "LOCAL-ONLY-example-admin-password-2026"
NON_ADMIN_EMAIL = "viewer@local.dev"
NON_ADMIN_PASSWORD = "LOCAL-ONLY-example-viewer-password-2026"
EXPECTED_REFRESH_COOKIE = "hybridinference_example_refresh"
EXPECTED_MODEL = "example-chat"
EXPECTED_CONTENT = os.getenv("EXAMPLE_EXPECTED_CONTENT", "RUNNABLE_EXAMPLE_OK")
SUCCESS_MARKER = "EXAMPLE_FULL_SMOKE_OK"
RESET_SUCCESS_MARKER = "EXAMPLE_RESET_SMOKE_OK"
REPO_ROOT = Path(__file__).resolve().parents[2]


class SmokeError(RuntimeError):
    """A safe-to-display failure that never contains response bodies."""


@dataclass(frozen=True)
class DemoState:
    """Secrets retained in memory across the ordinary backend recreate."""

    access_token: str
    api_key: str
    key_prefix: str
    user_id: str
    completion_id: str
    request_id: str
    refresh_cookies: CookieJar


@dataclass(frozen=True)
class ResetStateFile:
    """A validated private reset-proof file and the credentials it contains."""

    path: str
    password: str
    api_key: str
    device: int
    inode: int


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    bearer: str | None = None,
    cookie_jar: CookieJar | None = None,
    expected_statuses: tuple[int, ...] = (200,),
) -> tuple[int, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )

    opener = (
        urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
        if cookie_jar is not None
        else None
    )
    try:
        response = (
            opener.open(request, timeout=10)
            if opener is not None
            else urllib.request.urlopen(request, timeout=10)
        )
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    else:
        with response:
            status = response.status
            raw = response.read()

    if status not in expected_statuses:
        raise SmokeError(f"{path} returned HTTP {status}")
    if not raw:
        return status, {}
    try:
        return status, json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SmokeError(f"{path} did not return JSON") from exc


def _request_page(base_url: str, path: str) -> None:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "text/html"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            content_type = response.headers.get_content_type()
            response.read(1)
    except urllib.error.HTTPError as exc:
        exc.read()
        raise SmokeError(f"{path} returned HTTP {exc.code}") from exc
    if content_type != "text/html":
        raise SmokeError(f"{path} was not served by the frontend")


def _request_sse(
    base_url: str,
    path: str,
    *,
    payload: dict[str, Any],
    bearer: str,
) -> list[str]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer}",
        },
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as exc:
        exc.read()
        raise SmokeError(f"{path} returned HTTP {exc.code}") from exc

    events: list[str] = []
    data_lines: list[str] = []

    def flush_event() -> None:
        if data_lines:
            events.append("\n".join(data_lines))
            data_lines.clear()

    with response:
        if response.status != 200:
            raise SmokeError(f"{path} returned HTTP {response.status}")
        if response.headers.get_content_type() != "text/event-stream":
            raise SmokeError(f"{path} did not return an SSE stream")
        for raw_line in response:
            try:
                line = raw_line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                raise SmokeError(f"{path} returned invalid SSE text") from exc
            if not line:
                flush_event()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
            elif line.startswith(":"):
                continue
        flush_event()
    return events


def _assert_sse_contract(events: list[str], *, path: str) -> None:
    if not events or events[-1] != "[DONE]":
        raise SmokeError(f"{path} did not end with [DONE]")
    if "[DONE]" in events[:-1]:
        raise SmokeError(f"{path} emitted data after [DONE]")

    chunks: list[dict[str, Any]] = []
    for event in events[:-1]:
        try:
            chunk = json.loads(event)
        except json.JSONDecodeError as exc:
            raise SmokeError(f"{path} emitted invalid JSON SSE data") from exc
        if not isinstance(chunk, dict):
            raise SmokeError(f"{path} emitted a non-object SSE chunk")
        chunks.append(chunk)
    completion_chunks = [
        chunk for chunk in chunks if chunk.get("object") == "chat.completion.chunk"
    ]
    if not completion_chunks:
        raise SmokeError(f"{path} emitted no completion chunks")

    # Playground also emits a sanitized routing-metadata SSE object whose
    # choices are empty and which is not a chat.completion.chunk. Every actual
    # completion chunk must still carry the one response-wide id.
    chunk_ids = [chunk.get("id") for chunk in completion_chunks]
    if not all(isinstance(chunk_id, str) and chunk_id for chunk_id in chunk_ids):
        raise SmokeError(f"{path} emitted a chunk without an id")
    if len(set(chunk_ids)) != 1:
        raise SmokeError(f"{path} changed completion id within one stream")

    content_parts: list[str] = []
    for chunk in completion_chunks:
        for choice in chunk.get("choices", []):
            content = choice.get("delta", {}).get("content")
            if isinstance(content, str):
                content_parts.append(content)
    if "".join(content_parts) != EXPECTED_CONTENT:
        raise SmokeError(f"{path} did not stream the expected provider response")


def _stream_completion(base_url: str, api_key: str) -> None:
    path = "/v1/chat/completions"
    events = _request_sse(
        base_url,
        path,
        payload={
            "model": EXPECTED_MODEL,
            "messages": [{"role": "user", "content": "Return the example sentinel."}],
            "stream": True,
        },
        bearer=api_key,
    )
    _assert_sse_contract(events, path=path)


def _stream_playground(base_url: str, access_token: str) -> None:
    path = "/internal/playground/chat"
    events = _request_sse(
        base_url,
        path,
        payload={
            "model": EXPECTED_MODEL,
            "system_prompt": "",
            "messages": [{"role": "user", "content": "Return the example sentinel."}],
            "temperature": 0,
            "max_tokens": 64,
        },
        bearer=access_token,
    )
    _assert_sse_contract(events, path=path)


def _check_public_surface(base_url: str) -> None:
    _, health = _request_json(base_url, "/health")
    if health.get("status") != "healthy":
        raise SmokeError("health is not healthy")
    if health.get("database_configured") is not True:
        raise SmokeError("health does not report a configured database")
    if health.get("database_connected") is not True:
        raise SmokeError("health does not report a connected database")

    _, site = _request_json(base_url, "/site-config")
    if site.get("distribution", {}).get("id") != "example":
        raise SmokeError("the example demo manifest is not active")
    if site.get("features", {}).get("public_signup") is not True:
        raise SmokeError("the example demo manifest does not expose signup")
    if site.get("site", {}).get("public_base_url", "").rstrip("/") != base_url.rstrip("/"):
        raise SmokeError("the example demo publishes the wrong frontend URL")

    _, models = _request_json(base_url, "/v1/models")
    served = {item.get("id") for item in models.get("data", [])}
    if EXPECTED_MODEL not in served:
        raise SmokeError("the example model is not served")

    for path in (
        "/",
        "/login",
        "/signup",
        "/dashboard/admin",
        "/dashboard/playground",
    ):
        _request_page(base_url, path)


def _wait_for_public_surface(base_url: str, deadline: float) -> None:
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            _check_public_surface(base_url)
        except (OSError, SmokeError, ValueError) as exc:
            last_error = exc
            time.sleep(1)
            continue
        return
    raise SmokeError(f"full demo did not become ready: {last_error}")


def _login(
    base_url: str,
    email: str,
    password: str,
    *,
    cookie_jar: CookieJar | None = None,
) -> tuple[int, dict[str, Any]]:
    status, data = _request_json(
        base_url,
        "/auth/login",
        method="POST",
        payload={"email": email, "password": password},
        cookie_jar=cookie_jar,
        expected_statuses=(200, 401),
    )
    if not isinstance(data, dict):
        raise SmokeError("login returned an invalid response")
    return status, data


def _login_or_signup(
    base_url: str,
    password: str,
    *,
    expect_existing: bool,
    cookie_jar: CookieJar,
) -> tuple[dict[str, Any], bool]:
    """Login first; bootstrap only the absent deterministic local account."""
    status, login = _login(
        base_url,
        ADMIN_EMAIL,
        password,
        cookie_jar=cookie_jar,
    )
    if status == 200:
        return login, True
    if expect_existing:
        raise SmokeError("the persisted demo admin could not log in")

    # Login deliberately returns the same 401 for an absent user and a wrong
    # password. With the checked-in deterministic default, 401 means first run;
    # a changed override against an existing account is caught by signup's 409
    # and reported without echoing either credential.
    signup_status, _ = _request_json(
        base_url,
        "/auth/signup",
        method="POST",
        payload={
            "email": ADMIN_EMAIL,
            "password": password,
            "user_name": "Local Admin",
            "use_case": "Full local example",
            "accepted_tos": True,
        },
        expected_statuses=(201, 409),
    )
    if signup_status == 409:
        raise SmokeError(
            "the demo admin already exists but login failed; use its original "
            "EXAMPLE_DEMO_ADMIN_PASSWORD or run make demo-reset"
        )
    status, login = _login(
        base_url,
        ADMIN_EMAIL,
        password,
        cookie_jar=cookie_jar,
    )
    if status != 200:
        raise SmokeError("the new demo admin could not log in")
    return login, False


def _assert_example_refresh_cookie(cookie_jar: CookieJar) -> None:
    if not any(cookie.name == EXPECTED_REFRESH_COOKIE for cookie in cookie_jar):
        raise SmokeError("login did not set the example-scoped refresh cookie")


def _check_non_admin_account(base_url: str, *, expect_existing: bool) -> None:
    """Prove the example's ADMIN_EMAILS wiring does not elevate other users."""
    cookie_jar = CookieJar()
    status, login = _login(
        base_url,
        NON_ADMIN_EMAIL,
        NON_ADMIN_PASSWORD,
        cookie_jar=cookie_jar,
    )
    if status == 401:
        if expect_existing:
            raise SmokeError("the persisted non-admin demo user could not log in")
        signup_status, _ = _request_json(
            base_url,
            "/auth/signup",
            method="POST",
            payload={
                "email": NON_ADMIN_EMAIL,
                "password": NON_ADMIN_PASSWORD,
                "user_name": "Local Viewer",
                "use_case": "Full local example authorization check",
                "accepted_tos": True,
            },
            expected_statuses=(201, 409),
        )
        if signup_status == 409:
            raise SmokeError("the non-admin demo user exists but login failed")
        status, login = _login(
            base_url,
            NON_ADMIN_EMAIL,
            NON_ADMIN_PASSWORD,
            cookie_jar=cookie_jar,
        )
    if status != 200:
        raise SmokeError("the non-admin demo user could not log in")

    user = login.get("user", {})
    access_token = login.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise SmokeError("non-admin login returned no access token")
    if user.get("email") != NON_ADMIN_EMAIL:
        raise SmokeError("non-admin login returned the wrong user")
    if user.get("role") == "admin" or user.get("is_admin") is not False:
        raise SmokeError("a non-configured demo email was promoted to admin")
    _assert_example_refresh_cookie(cookie_jar)

    admin_status, _ = _request_json(
        base_url,
        "/admin/stats",
        bearer=access_token,
        expected_statuses=(403,),
    )
    if admin_status != 403:
        raise SmokeError("a non-admin demo user reached the Admin API")


def _validate_admin_login(login: dict[str, Any]) -> tuple[str, str]:
    access_token = login.get("access_token")
    user = login.get("user", {})
    user_id = user.get("id")
    if not isinstance(access_token, str) or not access_token:
        raise SmokeError("login did not return an access token")
    if not isinstance(user_id, str) or not user_id:
        raise SmokeError("login did not return a user id")
    if user.get("email") != ADMIN_EMAIL:
        raise SmokeError("login returned the wrong demo user")
    if user.get("role") != "admin" or user.get("is_admin") is not True:
        raise SmokeError("the first demo account was not promoted to admin")
    return access_token, user_id


def _check_current_admin(
    base_url: str,
    access_token: str,
    *,
    expected_user_id: str | None = None,
) -> str:
    _, user = _request_json(base_url, "/user/me", bearer=access_token)
    user_id = user.get("id")
    if not isinstance(user_id, str) or not user_id:
        raise SmokeError("/user/me returned no user id")
    if expected_user_id is not None and user_id != expected_user_id:
        raise SmokeError("/user/me returned a different demo account")
    if user.get("email") != ADMIN_EMAIL:
        raise SmokeError("/user/me returned the wrong demo user")
    if user.get("role") != "admin" or user.get("is_admin") is not True:
        raise SmokeError("/user/me does not report the demo account as admin")
    return user_id


def _active_key(data: Any) -> tuple[str, str] | None:
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise SmokeError("API key listing returned an invalid response")
    active = [item for item in data["keys"] if item.get("status") == "active"]
    if not active:
        return None
    key = active[0].get("api_key")
    prefix = active[0].get("key_prefix")
    if not isinstance(key, str) or not key:
        raise SmokeError("the active API key could not be decrypted")
    if not isinstance(prefix, str) or not prefix:
        raise SmokeError("the active API key has no prefix")
    return key, prefix


def _get_or_create_key(base_url: str, access_token: str) -> tuple[str, str]:
    _, listed = _request_json(base_url, "/user/api-keys/all", bearer=access_token)
    existing = _active_key(listed)
    if existing is not None:
        return existing

    _, created = _request_json(
        base_url,
        "/user/api-keys",
        method="POST",
        payload={},
        bearer=access_token,
        expected_statuses=(201,),
    )
    api_key = created.get("api_key")
    key_prefix = created.get("key_prefix")
    if not isinstance(api_key, str) or not api_key:
        raise SmokeError("API key creation returned no key")
    if not isinstance(key_prefix, str) or not key_prefix:
        raise SmokeError("API key creation returned no prefix")
    return api_key, key_prefix


def _assert_anonymous_is_rejected(base_url: str) -> None:
    status, _ = _request_json(
        base_url,
        "/v1/chat/completions",
        method="POST",
        payload={
            "model": EXPECTED_MODEL,
            "messages": [{"role": "user", "content": "Return the example sentinel."}],
        },
        expected_statuses=(401,),
    )
    if status != 401:
        raise SmokeError("anonymous completion was not rejected")


def _completion(base_url: str, api_key: str) -> str:
    _, completion = _request_json(
        base_url,
        "/v1/chat/completions",
        method="POST",
        payload={
            "model": EXPECTED_MODEL,
            "messages": [{"role": "user", "content": "Return the example sentinel."}],
        },
        bearer=api_key,
    )
    content = completion.get("choices", [{}])[0].get("message", {}).get("content")
    if content != EXPECTED_CONTENT:
        raise SmokeError("completion did not traverse the configured provider")
    completion_id = completion.get("id")
    if not isinstance(completion_id, str) or not completion_id:
        raise SmokeError("completion returned no id")
    return completion_id


def _check_admin_surfaces(base_url: str, access_token: str) -> None:
    _, stats = _request_json(base_url, "/admin/stats", bearer=access_token)
    # The endpoint exposes one row per hourly aggregate; a fresh example has
    # no rows yet, so an empty list is still the valid Admin API contract.
    if not isinstance(stats.get("stats"), list):
        raise SmokeError("the Admin Console stats API is unavailable")

    _, playground = _request_json(
        base_url,
        "/internal/playground/models",
        bearer=access_token,
    )
    models = {item.get("id") for item in playground.get("models", [])}
    if EXPECTED_MODEL not in models:
        raise SmokeError("the Playground does not expose the example model")


def _wait_for_history(
    base_url: str,
    access_token: str,
    deadline: float,
    *,
    required_request_id: str | None = None,
    excluded_request_ids: set[str] | None = None,
) -> str:
    last_error: Exception | None = None
    path = f"/user/recent-requests?model_id={EXPECTED_MODEL}"
    while time.monotonic() < deadline:
        try:
            _, history = _request_json(base_url, path, bearer=access_token)
            requests = history.get("requests", [])
            matching = [
                item
                for item in requests
                if item.get("model_id") == EXPECTED_MODEL and item.get("status_code") == 200
            ]
            if required_request_id is not None:
                matching = [
                    item for item in matching if item.get("request_id") == required_request_id
                ]
            if excluded_request_ids:
                matching = [
                    item for item in matching if item.get("request_id") not in excluded_request_ids
                ]
            if matching:
                request_id = matching[0].get("request_id")
                if isinstance(request_id, str) and request_id:
                    return request_id
            last_error = SmokeError("request history has not recorded the completion yet")
        except (OSError, SmokeError, ValueError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise SmokeError(f"request history did not become visible: {last_error}")


def _history_request_ids(base_url: str, access_token: str) -> set[str]:
    _, history = _request_json(
        base_url,
        f"/user/recent-requests?model_id={EXPECTED_MODEL}",
        bearer=access_token,
    )
    return {
        item["request_id"]
        for item in history.get("requests", [])
        if item.get("model_id") == EXPECTED_MODEL
        and item.get("status_code") == 200
        and isinstance(item.get("request_id"), str)
        and item["request_id"]
    }


def _prepare(
    base_url: str,
    password: str,
    deadline: float,
    *,
    expect_existing: bool,
) -> DemoState:
    _wait_for_public_surface(base_url, deadline)
    _assert_anonymous_is_rejected(base_url)
    refresh_cookies = CookieJar()
    login, _ = _login_or_signup(
        base_url,
        password,
        expect_existing=expect_existing,
        cookie_jar=refresh_cookies,
    )
    _assert_example_refresh_cookie(refresh_cookies)
    access_token, user_id = _validate_admin_login(login)
    _check_current_admin(
        base_url,
        access_token,
        expected_user_id=user_id,
    )
    api_key, key_prefix = _get_or_create_key(base_url, access_token)
    _check_admin_surfaces(base_url, access_token)
    _check_non_admin_account(base_url, expect_existing=expect_existing)
    prior_request_ids = _history_request_ids(base_url, access_token)
    completion_id = _completion(base_url, api_key)
    request_id = _wait_for_history(
        base_url,
        access_token,
        deadline,
        excluded_request_ids=prior_request_ids,
    )
    _stream_completion(base_url, api_key)
    _stream_playground(base_url, access_token)
    return DemoState(
        access_token=access_token,
        api_key=api_key,
        key_prefix=key_prefix,
        user_id=user_id,
        completion_id=completion_id,
        request_id=request_id,
        refresh_cookies=refresh_cookies,
    )


def _run_recreate(command: list[str], deadline: float) -> None:
    if not command:
        raise SmokeError("no backend recreate command was supplied")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SmokeError("the smoke timeout elapsed before backend recreate")
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeError("backend recreate timed out") from exc
    if proc.returncode != 0:
        # Compose diagnostics may contain interpolated environment values.
        # Keep them out of this secret-safe smoke output; `make logs` remains
        # available when the operator needs the underlying container failure.
        raise SmokeError("backend recreate failed; inspect make logs")


def _verify_after_recreate(
    base_url: str,
    password: str,
    state: DemoState,
    deadline: float,
) -> None:
    _wait_for_public_surface(base_url, deadline)

    # The original access JWT must still authorize a DB-backed request after
    # process replacement, proving the JWT secret and account rows persisted.
    _, listed = _request_json(
        base_url,
        "/user/api-keys/all",
        bearer=state.access_token,
    )
    persisted = _active_key(listed)
    if persisted is None:
        raise SmokeError("the original API key disappeared after backend recreate")
    persisted_key, persisted_prefix = persisted
    if persisted_prefix != state.key_prefix or not hmac.compare_digest(
        persisted_key, state.api_key
    ):
        raise SmokeError("the original API key changed after backend recreate")

    # The original host-only refresh cookie must also work through the
    # frontend origin. This reaches the persisted auth_sessions row, rotates
    # the cookie, and proves the browser session contract rather than merely
    # reusing a still-valid access JWT.
    _, refreshed = _request_json(
        base_url,
        "/auth/refresh",
        method="POST",
        cookie_jar=state.refresh_cookies,
    )
    refreshed_access_token = refreshed.get("access_token")
    if not isinstance(refreshed_access_token, str) or not refreshed_access_token:
        raise SmokeError("refresh after backend recreate returned no access token")
    _assert_example_refresh_cookie(state.refresh_cookies)
    _check_current_admin(
        base_url,
        refreshed_access_token,
        expected_user_id=state.user_id,
    )

    login_status, login = _login(base_url, ADMIN_EMAIL, password)
    if login_status != 200:
        raise SmokeError("the demo admin could not log in after backend recreate")
    new_access_token, user_id = _validate_admin_login(login)
    if user_id != state.user_id:
        raise SmokeError("backend recreate replaced the demo admin account")
    _check_current_admin(
        base_url,
        new_access_token,
        expected_user_id=state.user_id,
    )

    _check_admin_surfaces(base_url, new_access_token)
    _wait_for_history(
        base_url,
        new_access_token,
        deadline,
        required_request_id=state.request_id,
    )
    _completion(base_url, state.api_key)


def _env_flag(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value in ("", "0", "false", "no"):
        return False
    if value in ("1", "true", "yes"):
        return True
    raise SmokeError(f"{name} must be a boolean value")


def _state_path(raw_path: str) -> str:
    if not os.path.isabs(raw_path):
        raise SmokeError("reset state path must be absolute")
    raw = Path(raw_path)
    if not raw.name:
        raise SmokeError("reset state path must name a file")
    # Resolve the parent so a parent-directory symlink cannot smuggle the
    # state into the checkout, but preserve the final component: resolving the
    # leaf here would make O_NOFOLLOW inspect the symlink target instead of the
    # caller-supplied path.
    path = os.path.join(os.path.realpath(raw.parent), raw.name)
    checkout = os.path.realpath(REPO_ROOT)
    if os.path.commonpath((path, checkout)) == checkout:
        raise SmokeError("reset state must live outside the checkout")
    return path


def _unlink_if_same_file(path: str, *, device: int, inode: int) -> None:
    """Delete only the exact regular file previously opened by this process."""
    try:
        current = os.lstat(path)
    except OSError:
        return
    if stat.S_ISREG(current.st_mode) and current.st_dev == device and current.st_ino == inode:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _write_reset_state(raw_path: str, password: str, state: DemoState) -> None:
    """Persist the minimum reset proof in an exclusive mode-0600 file."""
    path = _state_path(raw_path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SmokeError("could not create the reset state file") from exc
    metadata: os.stat_result | None = None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise SmokeError("reset state file is not a private regular file")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(
                {
                    "version": 1,
                    "password": password,
                    "api_key": state.api_key,
                },
                handle,
            )
    except Exception:
        if fd >= 0:
            os.close(fd)
        if metadata is not None:
            _unlink_if_same_file(
                path,
                device=metadata.st_dev,
                inode=metadata.st_ino,
            )
        raise


def _read_reset_state(raw_path: str) -> ResetStateFile:
    path = _state_path(raw_path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SmokeError("could not open the reset state file") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise SmokeError("reset state file is not a private regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            data = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SmokeError("reset state file is invalid") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SmokeError("reset state file has an unsupported version")
    password = data.get("password")
    api_key = data.get("api_key")
    if not isinstance(password, str) or not password:
        raise SmokeError("reset state file has no password")
    if not isinstance(api_key, str) or not api_key:
        raise SmokeError("reset state file has no API key")
    return ResetStateFile(
        path=path,
        password=password,
        api_key=api_key,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


def _verify_after_reset(base_url: str, raw_path: str, deadline: float) -> None:
    state = _read_reset_state(raw_path)
    try:
        _wait_for_public_surface(base_url, deadline)
        login_status, _ = _login(base_url, ADMIN_EMAIL, state.password)
        if login_status != 401:
            raise SmokeError("the old demo account survived destructive reset")
        status, _ = _request_json(
            base_url,
            "/v1/chat/completions",
            method="POST",
            payload={
                "model": EXPECTED_MODEL,
                "messages": [{"role": "user", "content": "Check the old key."}],
            },
            bearer=state.api_key,
            expected_statuses=(401,),
        )
        if status != 401:
            raise SmokeError("the old API key survived destructive reset")
    finally:
        _unlink_if_same_file(
            state.path,
            device=state.device,
            inode=state.inode,
        )


def main() -> None:
    """Run the two-phase full-demo smoke without exposing credentials."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:13001")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--write-reset-state")
    parser.add_argument("--expect-existing-state")
    parser.add_argument("--verify-reset-state")
    parser.add_argument("--recreate-command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout
    if args.verify_reset_state:
        if args.write_reset_state or args.expect_existing_state or args.recreate_command:
            parser.error("--verify-reset-state cannot be combined with smoke options")
        try:
            _verify_after_reset(args.base_url, args.verify_reset_state, deadline)
        except (OSError, SmokeError, ValueError) as exc:
            raise SystemExit(f"full example reset smoke failed: {exc}") from None
        print(RESET_SUCCESS_MARKER)
        return
    if not args.recreate_command:
        parser.error("--recreate-command is required for the full smoke")
    if args.write_reset_state and args.expect_existing_state:
        parser.error("reset state cannot be written and consumed in the same smoke")

    try:
        expected_state = (
            _read_reset_state(args.expect_existing_state) if args.expect_existing_state else None
        )
        password = (
            expected_state.password
            if expected_state is not None
            else os.getenv("EXAMPLE_DEMO_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
        )
        expect_existing = expected_state is not None or _env_flag("EXAMPLE_DEMO_EXPECT_EXISTING")
        state = _prepare(
            args.base_url,
            password,
            deadline,
            expect_existing=expect_existing,
        )
        if expected_state is not None and not hmac.compare_digest(
            state.api_key,
            expected_state.api_key,
        ):
            raise SmokeError("the original API key changed after stop and resume")
        _run_recreate(args.recreate_command, deadline)
        _verify_after_recreate(args.base_url, password, state, deadline)
        if args.write_reset_state:
            _write_reset_state(args.write_reset_state, password, state)
    except (OSError, SmokeError, ValueError) as exc:
        raise SystemExit(f"full example smoke failed: {exc}") from None
    print(SUCCESS_MARKER)


if __name__ == "__main__":
    main()
