#!/usr/bin/env python3
r"""Black-box acceptance of the unified batch API against a running gateway.

Everything here is synthetic: a fake upstream that answers both the OpenAI
chat and the Anthropic Messages protocols and records what it was sent, a
models file routing one OpenAI-compatible and one native Anthropic model to
it, and two throwaway accounts. No real provider, account or shared database
is involved, so a pass proves the gateway's protocol and storage behaviour,
not a provider's.

Subcommands (run each with ``--help``):

``upstream``
    Serve the fake provider (``POST /v1/chat/completions``, ``POST
    /v1/messages``; ``GET /_calls`` lists what arrived, with each request's
    ``anthropic-version`` header) and a webhook receiver (``POST /_hook``,
    listed by ``GET /_hooks``). Webhooks to it need the gateway's
    ``BATCH_WEBHOOK_ALLOW_PRIVATE_TARGETS`` on: the receiver is a loopback address.
``models``
    Write a models file routing ``smoke-chat`` (OpenAI-compatible) and
    ``smoke-claude`` (native Anthropic) to the fake upstream.
``seed``
    Create an ``internal`` owner and a second ``internal`` user with an API key
    each, through the gateway's own store code, in the gateway's database. Run
    it with the gateway's database and ``API_KEY_SECRET`` environment.
``webhook``
    Point the owner's batch webhook at the fake upstream's receiver.
``check``
    The acceptance checks: the retired routes' ``410`` (and ``404`` with
    ``BATCH_ENABLED`` off), online Messages, Messages batches inline and from
    a file under the ``anthropic-version`` header, refusals (the obsolete
    body field included), the OpenAI workflows, idempotency (a version the
    operator stopped allowing still replays), ownership, cancellation, the
    admin status filter, webhook event types and deletion. With
    ``--legacy-ids``, batches the retired Message Batches API created (see
    ``legacy-create``) are read and downloaded too, and their webhook events
    checked when a webhook was set up before they ended.
``legacy-create``
    Against a gateway that still serves ``/v1/messages/batches`` (the previous
    release), create a Message Batch the old way and print its id.
``stage`` / ``expect-untouched`` / ``expect-complete``
    A rolling upgrade, in three steps: on this version, pause the Messages
    model and create Messages batches through ``/v1/batches`` (inline and from
    a file); on a previous-release gateway sharing the database (this
    version's processes stopped), unpause the model and confirm that none of
    those batches is run, settled or expired; back on this version, confirm
    they complete.

A typical local run, every port and path your own::

    python ops/dev/batch_api_smoke.py upstream --port 18090 &
    python ops/dev/batch_api_smoke.py models --upstream http://127.0.0.1:18090 --out /tmp/m.yaml
    # start the gateway with MODELS_CONFIG_PATH=/tmp/m.yaml, ADMIN_TOKEN=..., its own database
    python ops/dev/batch_api_smoke.py seed > /tmp/keys.json
    python ops/dev/batch_api_smoke.py check --gateway http://127.0.0.1:18091 \\
        --upstream http://127.0.0.1:18090 --admin-token ... --keys /tmp/keys.json

Exit status 0 and a final ``BATCH_API_SMOKE_OK`` line mean every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

CHAT_MODEL = "smoke-chat"
MESSAGES_MODEL = "smoke-claude"
OK_MARKER = "BATCH_API_SMOKE_OK"
VERSION_HEADER = "anthropic-version"
MIGRATION_DOC = "https://doc.hybridinference.org/batches.html#moving-from-message-batches"
_FINAL = ("completed", "failed", "expired", "cancelled")


class SmokeError(AssertionError):
    """One acceptance check failed."""


def _expect(condition: bool, what: str) -> None:
    if not condition:
        raise SmokeError(what)
    print(f"PASS {what}", flush=True)


# -- fake upstream -----------------------------------------------------------------


def _serve_upstream(port: int) -> None:
    from aiohttp import web

    calls: list[dict[str, Any]] = []

    def _text(content: Any) -> str:
        if isinstance(content, str):
            return content
        return " ".join(part.get("text", "") for part in content or [] if isinstance(part, dict))

    async def chat(request: web.Request) -> web.Response:
        body = await request.json()
        prompt = _text(body["messages"][-1]["content"])
        calls.append({"path": "/v1/chat/completions", "model": body.get("model"), "text": prompt})
        return web.json_response(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"echo {prompt}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )

    async def messages(request: web.Request) -> web.Response:
        body = await request.json()
        prompt = _text(body["messages"][-1]["content"])
        calls.append(
            {
                "path": "/v1/messages",
                "model": body.get("model"),
                "text": prompt,
                "anthropic_version": request.headers.get("anthropic-version"),
                "keys": sorted(body),
            }
        )
        return web.json_response(
            {
                "id": f"msg_{uuid.uuid4().hex[:8]}",
                "type": "message",
                "role": "assistant",
                "model": body.get("model"),
                "content": [{"type": "text", "text": f"echo {prompt}"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )

    async def list_calls(_request: web.Request) -> web.Response:
        return web.json_response(calls)

    hooks: list[dict[str, Any]] = []

    async def hook(request: web.Request) -> web.Response:
        hooks.append(json.loads(await request.read()))
        return web.Response(status=204)

    async def list_hooks(_request: web.Request) -> web.Response:
        return web.json_response(hooks)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_post("/v1/messages", messages)
    app.router.add_get("/_calls", list_calls)
    app.router.add_post("/_hook", hook)
    app.router.add_get("/_hooks", list_hooks)
    web.run_app(app, host="127.0.0.1", port=port, print=lambda *_: None)


def _write_models(upstream: str, out: Path) -> None:
    base = upstream.rstrip("/")
    common = (
        "    required_role: free\n"
        "    context_length: 8192\n"
        "    max_output_length: 1024\n"
        '    input_modalities: ["text"]\n'
        '    output_modalities: ["text"]\n'
        "    pricing:\n"
        '      prompt: "1"\n'
        '      completion: "2"\n'
        '      image: "0"\n'
        '      request: "0"\n'
    )
    out.write_text(
        "models:\n"
        f"  - id: {CHAT_MODEL}\n"
        "    name: Smoke Chat\n"
        "    provider: openai_compat\n"
        f"{common}"
        "    route:\n"
        "      - kind: openai_compat\n"
        "        weight: 1.0\n"
        f"        base_url: {base}/v1\n"
        "        api_key: smoke-not-a-real-key\n"
        f"  - id: {MESSAGES_MODEL}\n"
        "    name: Smoke Claude\n"
        "    provider: anthropic\n"
        f"{common}"
        "    route:\n"
        "      - kind: anthropic\n"
        "        weight: 1.0\n"
        f"        base_url: {base}\n"
        "        api_key: smoke-not-a-real-key\n"
    )
    print(out)


async def _seed() -> dict[str, str]:
    import os
    from decimal import Decimal

    import asyncpg

    from serving.servers.auth import generate_api_key, hash_api_key
    from serving.storage.postgres_operational import PostgresOperationalStore

    pool = await asyncpg.create_pool(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        min_size=1,
        max_size=2,
    )
    try:
        store = PostgresOperationalStore(pool)
        keys: dict[str, str] = {}
        for name in ("owner", "stranger"):
            tag = uuid.uuid4().hex[:8]
            user_id = f"smoke-{name}-{tag}"
            await store.create_user(
                user_id=user_id,
                email=f"{name}-{tag}@smoke.invalid",
                password_hash="x",
                email_verified=True,
            )
            await store.update_user_fields(user_id, role="internal")
            key = generate_api_key()
            await store.create_key(
                key_hash=hash_api_key(key),
                key_prefix=key[:12],
                user_id=user_id,
                account_id=user_id,
                quota_daily_cost_usd=Decimal("100.00"),
            )
            keys[f"{name}_key"] = key
            keys[f"{name}_id"] = user_id
        return keys
    finally:
        await pool.close()


# -- the client side ---------------------------------------------------------------


class Gateway:
    """The gateway, as the API-key holders and the operator call it."""

    def __init__(self, base: str, *, owner: str, stranger: str | None, admin: str | None):
        import httpx

        self.base = base.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=60)
        self.owner = {"Authorization": f"Bearer {owner}"}
        self.stranger = {"Authorization": f"Bearer {stranger}"} if stranger else None
        self.admin = {"Authorization": f"Bearer {admin}"} if admin else None
        self.owner_key = owner

    def create(self, body: dict[str, Any], version: str | None = None, **headers: str) -> Any:
        """``POST /v1/batches`` as the owner, with *version* as its ``anthropic-version``."""
        if version is not None:
            headers = {VERSION_HEADER: version, **headers}
        return self.http.post("/v1/batches", json=body, headers={**self.owner, **headers})

    def configure(self, key: str, value: Any) -> None:
        """Set a runtime setting through the admin API (None resets it to its default)."""
        if self.admin is None:
            raise SmokeError("--admin-token is needed to change a setting")
        if value is None:
            response = self.http.delete(f"/admin/config/{key}", headers=self.admin)
        else:
            response = self.http.patch(
                "/admin/config", json={"values": {key: value}}, headers=self.admin
            )
        if response.status_code >= 300:
            raise SmokeError(f"could not set {key}: {response.status_code}")

    def batch(self, batch_id: str) -> dict[str, Any]:
        """The owner's view of one batch."""
        response = self.http.get(f"/v1/batches/{batch_id}", headers=self.owner)
        response.raise_for_status()
        return response.json()

    def wait(self, batch_id: str, statuses: tuple[str, ...] = _FINAL, timeout: float = 90) -> Any:
        """Poll a batch until it shows one of *statuses*."""
        deadline = time.monotonic() + timeout
        while True:
            batch = self.batch(batch_id)
            if batch["status"] in statuses:
                return batch
            if time.monotonic() > deadline:
                raise SmokeError(f"{batch_id} still {batch['status']} after {timeout}s")
            time.sleep(0.5)

    def set_cap(self, model: str, cap: int | None) -> None:
        """Set (or with None, clear) a model's batch cap through the admin API."""
        if self.admin is None:
            raise SmokeError("--admin-token is needed to pause a model")
        response = self.http.patch(
            f"/admin/batch/models/{model}", json={"max_concurrency": cap}, headers=self.admin
        )
        if response.status_code >= 300:
            raise SmokeError(f"could not set the batch cap of {model}: {response.status_code}")

    def upload(self, lines: list[dict[str, Any]]) -> str:
        """Upload JSONL lines as a ``purpose=batch`` file; return its id."""
        payload = ("\n".join(json.dumps(line) for line in lines) + "\n").encode()
        response = self.http.post(
            "/v1/files",
            headers=self.owner,
            data={"purpose": "batch"},
            files={"file": ("input.jsonl", payload, "application/jsonl")},
        )
        response.raise_for_status()
        return response.json()["id"]


def _upstream_calls(upstream: str) -> list[dict[str, Any]]:
    import httpx

    return httpx.get(f"{upstream.rstrip('/')}/_calls", timeout=10).json()


def _messages_line(custom_id: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/messages",
        "body": {
            "model": MESSAGES_MODEL,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": text}],
            **extra,
        },
    }


def _chat_line(custom_id: str, text: str) -> dict[str, Any]:
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {"model": CHAT_MODEL, "messages": [{"role": "user", "content": text}]},
    }


_RETIRED = [
    (method, f"{prefix}/v1/messages/batches{suffix}")
    for prefix in ("", "/anthropic")
    for method, suffix in (
        ("POST", ""),
        ("GET", ""),
        ("GET", "/msgbatch_x"),
        ("POST", "/msgbatch_x/cancel"),
        ("DELETE", "/msgbatch_x"),
        ("GET", "/msgbatch_x/results"),
    )
]


def _hooks(upstream: str) -> list[dict[str, Any]]:
    import httpx

    return httpx.get(f"{upstream.rstrip('/')}/_hooks", timeout=10).json()


def _check(gw: Gateway, upstream: str, legacy_ids: list[str]) -> None:
    run = uuid.uuid4().hex[:6]

    # 1. The retired routes answer 410 with the way forward; online Messages is unchanged.
    paths = gw.http.get("/openapi.json").json()["paths"]
    _expect(not [p for p in paths if "messages/batches" in p], "no Message Batches path in OpenAPI")
    for method, path in _RETIRED:
        for who, headers in (
            ("the owner", {**gw.owner, VERSION_HEADER: "2023-06-01"}),
            ("an invalid key", {"Authorization": "Bearer not-a-key"}),
        ):
            response = gw.http.request(method, path, headers=headers, content=b"{not json")
            error = response.json().get("error", {}) if response.status_code == 410 else {}
            _expect(
                response.status_code == 410
                and error.get("type") == "invalid_request_error"
                and "/v1/batches" in error.get("message", "")
                and MIGRATION_DOC in error.get("message", ""),
                f"{method} {path} -> 410 with migration guidance, for {who}",
            )
    for method, path in (
        ("GET", "/v1/messages/batches/msgbatch_x/other"),
        ("PUT", "/v1/messages/batches/msgbatch_x"),
    ):
        response = gw.http.request(method, path, headers=gw.owner)
        _expect(response.status_code in (404, 405), f"{method} {path} is no tombstone")
    if gw.admin is not None:
        gw.configure("BATCH_ENABLED", False)
        try:
            for method, path in _RETIRED[:2]:
                response = gw.http.request(method, path, headers=gw.owner)
                _expect(response.status_code == 404, f"BATCH_ENABLED off: {method} {path} -> 404")
            response = gw.http.get("/v1/batches", headers=gw.owner)
            _expect(response.status_code == 404, "BATCH_ENABLED off: /v1/batches -> 404")
        finally:
            gw.configure("BATCH_ENABLED", True)
        _expect(gw.http.get("/v1/batches", headers=gw.owner).status_code == 200, "back on")
    for path in ("/v1/messages", "/anthropic/v1/messages"):
        online = gw.http.post(
            path,
            headers={**gw.owner, VERSION_HEADER: "2023-06-01"},
            json={
                "model": MESSAGES_MODEL,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": f"online {run}"}],
            },
        )
        _expect(
            online.status_code == 200 and online.json()["type"] == "message",
            f"online POST {path} still answers a message",
        )

    # 2. Refusals, before anything is stored or run.
    one = {"requests": [_messages_line("a", f"refused {run}")]}
    cases = {
        "no anthropic-version header": (gw.create(one), "needs this header"),
        "unknown version": (gw.create(one, "2099-01-01"), "not supported"),
        "the obsolete body field, beside a valid header": (
            gw.create({**one, "anthropic_version": "2023-06-01"}, "2023-06-01"),
            "not accepted in the body",
        ),
        "the obsolete body field, null": (
            gw.create({**one, "anthropic_version": None}, "2023-06-01"),
            "not accepted in the body",
        ),
        "a version header on a chat batch": (
            gw.create({"requests": [_chat_line("c", "x")]}, "2023-06-01"),
            "only a /v1/messages batch",
        ),
        "anthropic-beta": (
            gw.create(one, "2023-06-01", **{"anthropic-beta": "files-api-2025-04-14"}),
            "anthropic-beta",
        ),
        "a version's own rules": (
            gw.create(
                {"requests": [_messages_line("t", f"refused {run}", temperature=0.2)]},
                "2026-10-21",
            ),
            "temperature",
        ),
        "a server tool": (
            gw.create(
                {
                    "requests": [
                        _messages_line(
                            "s",
                            f"refused {run}",
                            tools=[{"type": "web_search_20250305", "name": "w"}],
                        )
                    ]
                },
                "2023-06-01",
            ),
            "not supported",
        ),
    }
    for what, (response, text) in cases.items():
        _expect(response.status_code == 400 and text in response.text, f"refused with 400: {what}")
    _expect(
        not [c for c in _upstream_calls(upstream) if c["text"].startswith(f"refused {run}")],
        "nothing refused reached the upstream",
    )

    # 3. An inline Messages batch, under 2026-10-21.
    created = gw.create(
        {
            "requests": [_messages_line(f"m-{i}", f"inline {run} {i}") for i in range(3)],
            "metadata": {"smoke": run},
        },
        "2026-10-21",
    )
    _expect(created.status_code == 200, "inline Messages batch created on /v1/batches")
    inline = created.json()
    _expect(
        inline["endpoint"] == "/v1/messages" and inline["anthropic_version"] == "2026-10-21",
        "the batch object names its target and frozen version",
    )
    done = gw.wait(inline["id"])
    _expect(done["status"] == "completed", "inline Messages batch completed")
    _expect(
        done["request_counts"] == {"total": 3, "completed": 3, "failed": 0},
        "inline Messages request_counts are the common ones",
    )
    export = gw.http.get(f"/v1/batches/{inline['id']}/output.jsonl", headers=gw.owner).text
    lines = [json.loads(line) for line in export.splitlines()]
    _expect(
        [line["custom_id"] for line in lines] == ["m-0", "m-1", "m-2"]
        and all(line["response"]["body"]["type"] == "message" for line in lines),
        "results are the common line shape with the Messages body",
    )
    sent = [c for c in _upstream_calls(upstream) if c["text"].startswith(f"inline {run}")]
    _expect(
        len(sent) == 3 and {c["anthropic_version"] for c in sent} == {"2026-10-21"},
        "each request reached the native upstream with anthropic-version 2026-10-21",
    )
    _expect(
        all("anthropic_version" not in c["keys"] for c in sent),
        "the version travelled as the header, not in the body",
    )

    # 4. A Messages batch from a file, through the OpenAI SDK, under 2023-06-01.
    file_id = gw.upload([_messages_line(f"f-{i}", f"file {run} {i}") for i in range(2)])
    try:
        from openai import OpenAI
    except ImportError:  # pragma: no cover - the SDK is a dev dependency
        OpenAI = None  # type: ignore[assignment,misc]
    if OpenAI is not None:
        sdk = OpenAI(base_url=f"{gw.base}/v1", api_key=gw.owner_key)
        file_batch = sdk.batches.create(
            input_file_id=file_id,
            endpoint="/v1/messages",  # type: ignore[arg-type]
            completion_window="24h",
            extra_headers={VERSION_HEADER: "2023-06-01"},
        ).id
    else:
        file_batch = gw.create(
            {"input_file_id": file_id, "endpoint": "/v1/messages", "completion_window": "24h"},
            "2023-06-01",
        ).json()["id"]
    done = gw.wait(file_batch)
    _expect(done["status"] == "completed", "Messages file batch completed (OpenAI SDK)")
    _expect(done["anthropic_version"] == "2023-06-01", "the file batch shows its frozen version")
    content = gw.http.get(f"/v1/files/{done['output_file_id']}/content", headers=gw.owner)
    _expect(len(content.text.splitlines()) == 2, "Messages output file has every result")
    sent = [c for c in _upstream_calls(upstream) if c["text"].startswith(f"file {run}")]
    _expect(
        len(sent) == 2 and {c["anthropic_version"] for c in sent} == {"2023-06-01"},
        "each file request reached the upstream with anthropic-version 2023-06-01",
    )

    # 5. A file with one line its version refuses runs nothing.
    bad_file = gw.upload(
        [
            _messages_line("ok", f"badfile {run} ok"),
            _messages_line("temp", f"badfile {run} temp", temperature=0.3),
        ]
    )
    bad = gw.create(
        {"input_file_id": bad_file, "endpoint": "/v1/messages", "completion_window": "24h"},
        "2026-10-21",
    ).json()
    failed = gw.wait(bad["id"])
    _expect(
        failed["status"] == "failed" and failed["errors"]["data"][0]["line"] == 2,
        "a file with a line its version refuses fails whole, naming the line",
    )
    _expect(
        not [c for c in _upstream_calls(upstream) if c["text"].startswith(f"badfile {run}")],
        "nothing of the invalid file reached the upstream",
    )

    # 6. The OpenAI workflows are unchanged.
    chat = gw.create({"requests": [_chat_line(f"c-{i}", f"chat {run} {i}") for i in range(2)]})
    _expect(gw.wait(chat.json()["id"])["status"] == "completed", "inline chat batch completed")
    responses = gw.create(
        {
            "requests": [
                {
                    "custom_id": "r",
                    "url": "/v1/responses",
                    "body": {"model": CHAT_MODEL, "input": f"responses {run}"},
                }
            ]
        }
    )
    _expect(
        gw.wait(responses.json()["id"])["status"] == "completed",
        "inline Responses batch completed",
    )
    chat_file = gw.upload([_chat_line(f"cf-{i}", f"chatfile {run} {i}") for i in range(2)])
    chat_batch = gw.create(
        {"input_file_id": chat_file, "endpoint": "/v1/chat/completions", "completion_window": "24h"}
    ).json()
    done = gw.wait(chat_batch["id"])
    _expect(
        done["status"] == "completed" and done["anthropic_version"] is None,
        "chat file batch completed, with no version",
    )

    # 7. Idempotency compares the version; a version no longer allowed still replays.
    gw.set_cap(MESSAGES_MODEL, 0)
    try:
        body = {"requests": [_messages_line("i", f"idem {run}")]}
        key = {"Idempotency-Key": f"smoke-{run}"}
        first = gw.create(body, "2026-10-21", **key)
        replay = gw.create(body, "2026-10-21", **key)
        other = gw.create(body, "2023-06-01", **key)
        _expect(
            first.status_code == replay.status_code == 200
            and first.json()["id"] == replay.json()["id"],
            "the same Idempotency-Key and version replay the batch",
        )
        _expect(other.status_code == 409, "the same key with another version answers 409")
        _expect(gw.create(body, **key).status_code == 400, "a replay without the header is 400")
        if gw.admin is not None:
            gw.configure("BATCH_ANTHROPIC_VERSIONS", "2023-06-01")
            try:
                still = gw.create(body, "2026-10-21", **key)
                fresh = gw.create(body, "2026-10-21", **{"Idempotency-Key": f"fresh-{run}"})
                _expect(
                    still.status_code == 200 and still.json()["id"] == first.json()["id"],
                    "a version the operator stopped allowing still replays its batch",
                )
                _expect(
                    fresh.status_code == 400 and "use one of 2023-06-01" in fresh.text,
                    "...but creates nothing new",
                )
            finally:
                gw.configure("BATCH_ANTHROPIC_VERSIONS", None)
        gw.http.post(f"/v1/batches/{first.json()['id']}/cancel", headers=gw.owner)
    finally:
        gw.set_cap(MESSAGES_MODEL, None)

    # 8. Ownership.
    if gw.stranger is not None:
        for method, path in (
            ("GET", f"/v1/batches/{inline['id']}"),
            ("POST", f"/v1/batches/{inline['id']}/cancel"),
            ("DELETE", f"/v1/batches/{inline['id']}"),
            ("GET", f"/v1/batches/{inline['id']}/results"),
            ("GET", f"/v1/batches/{inline['id']}/output.jsonl"),
        ):
            response = gw.http.request(method, path, headers=gw.stranger)
            _expect(response.status_code == 404, f"another user: {method} {path} -> 404")
        listed = gw.http.get("/v1/batches?limit=100", headers=gw.stranger).json()["data"]
        _expect(inline["id"] not in [row["id"] for row in listed], "another user's list omits it")

    # 9. Cancellation, with the model paused so the requests stay queued.
    gw.set_cap(MESSAGES_MODEL, 0)
    try:
        queued = gw.create(
            {"requests": [_messages_line(f"q-{i}", f"cancel {run} {i}") for i in range(3)]},
            "2023-06-01",
        ).json()
        cancelled = gw.http.post(f"/v1/batches/{queued['id']}/cancel", headers=gw.owner).json()
        _expect(cancelled["status"] == "cancelled", "a queued Messages batch is cancelled")
    finally:
        gw.set_cap(MESSAGES_MODEL, None)
    _expect(
        not [c for c in _upstream_calls(upstream) if c["text"].startswith(f"cancel {run}")],
        "nothing of the cancelled batch ran",
    )

    # 10. The admin overview filters on the status it shows.
    if gw.admin is not None:
        completed = gw.http.get(
            "/admin/batch/jobs?status=completed&limit=200", headers=gw.admin
        ).json()["data"]
        ids = [row["id"] for row in completed]
        _expect(inline["id"] in ids and file_batch in ids, "admin status=completed lists them")
        row = next(row for row in completed if row["id"] == inline["id"])
        _expect(
            (row["status"], row["input"], row["target"]) == ("completed", "inline", "messages"),
            "admin rows show the common status, the input method and the target",
        )
        cancelled_rows = gw.http.get(
            "/admin/batch/jobs?status=cancelled&limit=200", headers=gw.admin
        ).json()["data"]
        _expect(queued["id"] in [r["id"] for r in cancelled_rows], "admin status=cancelled")
        _expect(
            gw.http.get("/admin/batch/jobs?status=ended", headers=gw.admin).status_code == 422,
            "admin status=ended is not a status",
        )

    # 11. Webhook events, when a webhook points at the fake receiver.
    webhook = gw.http.get("/v1/batch_webhook", headers=gw.owner).json()
    receiving = webhook.get("configured") and webhook.get("url", "").endswith("/_hook")
    if receiving:
        deadline = time.monotonic() + 30
        want = {inline["id"]: "batch.completed", file_batch: "batch.completed"}
        want.update(dict.fromkeys(legacy_ids, "message_batch.ended"))
        while True:
            seen = {event["data"]["id"]: event["type"] for event in _hooks(upstream)}
            if all(batch_id in seen for batch_id in want) or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        for batch_id, event_type in want.items():
            _expect(seen.get(batch_id) == event_type, f"{batch_id} delivered {event_type}")

    # 12. Legacy Message Batches, through /v1/batches.
    for legacy in legacy_ids:
        batch = gw.wait(legacy)
        _expect(
            batch["endpoint"] == "/v1/messages" and batch["status"] == "completed",
            f"legacy {legacy} reads as a completed /v1/messages batch",
        )
        lines = gw.http.get(f"/v1/batches/{legacy}/output.jsonl", headers=gw.owner).text
        _expect(
            all(
                json.loads(line)["response"]["body"]["type"] == "message"
                for line in lines.splitlines()
            ),
            f"legacy {legacy} results download in the common line shape",
        )
        if gw.stranger is not None:
            response = gw.http.get(f"/v1/batches/{legacy}", headers=gw.stranger)
            _expect(response.status_code == 404, f"legacy {legacy} is not another user's")

    # 13. Deletion.
    deleted = gw.http.delete(f"/v1/batches/{inline['id']}", headers=gw.owner).json()
    _expect(deleted == {"id": inline["id"], "object": "batch.deleted", "deleted": True}, "deleted")
    _expect(
        gw.http.get(f"/v1/batches/{inline['id']}", headers=gw.owner).status_code == 404,
        "a deleted batch reads as not found",
    )


def _webhook(gw: Gateway, upstream: str) -> None:
    response = gw.http.put(
        "/v1/batch_webhook", headers=gw.owner, json={"url": f"{upstream.rstrip('/')}/_hook"}
    )
    _expect(response.status_code == 200, "the owner's webhook points at the fake receiver")


# -- rolling upgrade ---------------------------------------------------------------


def _stage(gw: Gateway, out: Path) -> None:
    run = uuid.uuid4().hex[:6]
    gw.set_cap(MESSAGES_MODEL, 0)
    inline = gw.create(
        {"requests": [_messages_line(f"r-{i}", f"rolling {run} inline {i}") for i in range(2)]},
        "2026-10-21",
    )
    _expect(inline.status_code == 200, "staged an inline Messages batch (model paused)")
    file_id = gw.upload([_messages_line(f"rf-{i}", f"rolling {run} file {i}") for i in range(2)])
    from_file = gw.create(
        {"input_file_id": file_id, "endpoint": "/v1/messages", "completion_window": "24h"},
        "2023-06-01",
    )
    _expect(from_file.status_code == 200, "staged a Messages file batch (model paused)")
    out.write_text(json.dumps({"run": run, "ids": [inline.json()["id"], from_file.json()["id"]]}))
    print(out)


def _expect_untouched(gw: Gateway, upstream: str, staged: dict[str, Any], seconds: float) -> None:
    # Unpaused, so that a worker which did not know these kinds would claim them now.
    gw.set_cap(MESSAGES_MODEL, None)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(1)
    for batch_id in staged["ids"]:
        batch = gw.batch(batch_id)
        counts = batch["request_counts"]
        _expect(
            batch["status"] in ("validating", "in_progress")
            and counts["completed"] == counts["failed"] == 0,
            f"{batch_id} untouched by this gateway's worker ({batch['status']}, {counts})",
        )
    ran = [c for c in _upstream_calls(upstream) if c["text"].startswith(f"rolling {staged['run']}")]
    _expect(not ran, "no request of a staged Messages batch reached the upstream")


def _expect_complete(gw: Gateway, upstream: str, staged: dict[str, Any]) -> None:
    gw.set_cap(MESSAGES_MODEL, None)
    for batch_id in staged["ids"]:
        _expect(gw.wait(batch_id)["status"] == "completed", f"{batch_id} completed on this version")
    ran = [c for c in _upstream_calls(upstream) if c["text"].startswith(f"rolling {staged['run']}")]
    _expect(len(ran) == 4, "every staged request ran exactly once")


def _legacy_create(gw: Gateway) -> None:
    response = gw.http.post(
        "/v1/messages/batches",
        headers={**gw.owner, "anthropic-version": "2023-06-01"},
        json={
            "requests": [
                {
                    "custom_id": f"legacy-{i}",
                    "params": {
                        "model": MESSAGES_MODEL,
                        "max_tokens": 16,
                        "messages": [{"role": "user", "content": f"legacy {i}"}],
                    },
                }
                for i in range(2)
            ]
        },
    )
    if response.status_code != 200:
        raise SmokeError(f"legacy create answered {response.status_code}: {response.text[:200]}")
    print(response.json()["id"])


# -- command line ------------------------------------------------------------------


def _gateway(args: argparse.Namespace) -> Gateway:
    keys = json.loads(Path(args.keys).read_text())
    return Gateway(
        args.gateway,
        owner=keys["owner_key"],
        stranger=keys.get("stranger_key"),
        admin=args.admin_token,
    )


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand; 0 when every check it made passed."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("upstream", help="serve the fake provider")
    up.add_argument("--port", type=int, required=True)
    models = sub.add_parser("models", help="write the smoke models file")
    models.add_argument("--upstream", required=True)
    models.add_argument("--out", type=Path, required=True)
    sub.add_parser("seed", help="create the two smoke accounts; prints their keys as JSON")
    for name in (
        "check",
        "stage",
        "expect-untouched",
        "expect-complete",
        "legacy-create",
        "webhook",
    ):
        command = sub.add_parser(name)
        command.add_argument("--gateway", required=True)
        command.add_argument("--keys", required=True, help="the JSON `seed` printed")
        command.add_argument("--admin-token")
        if name != "legacy-create":
            command.add_argument("--upstream", required=name != "stage")
        if name == "check":
            command.add_argument("--legacy-ids", nargs="*", default=[])
        if name in ("stage", "expect-untouched", "expect-complete"):
            command.add_argument("--staged", type=Path, required=True)
        if name == "expect-untouched":
            command.add_argument("--seconds", type=float, default=15.0)
    args = parser.parse_args(argv)

    if args.command == "upstream":
        _serve_upstream(args.port)
        return 0
    if args.command == "models":
        _write_models(args.upstream, args.out)
        return 0
    if args.command == "seed":
        print(json.dumps(asyncio.run(_seed())))
        return 0
    gw = _gateway(args)
    try:
        if args.command == "check":
            _check(gw, args.upstream, args.legacy_ids)
        elif args.command == "stage":
            _stage(gw, args.staged)
        elif args.command == "expect-untouched":
            _expect_untouched(gw, args.upstream, json.loads(args.staged.read_text()), args.seconds)
        elif args.command == "expect-complete":
            _expect_complete(gw, args.upstream, json.loads(args.staged.read_text()))
        elif args.command == "legacy-create":
            _legacy_create(gw)
        elif args.command == "webhook":
            _webhook(gw, args.upstream)
    except SmokeError as exc:
        print(f"FAIL {exc}", flush=True)
        return 1
    print(OK_MARKER, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
