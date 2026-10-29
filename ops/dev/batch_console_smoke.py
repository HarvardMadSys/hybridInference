#!/usr/bin/env python3
r"""Black-box acceptance of creating batches in the console, against a running gateway.

Everything is synthetic: a fake upstream answering ``/v1/chat/completions``
and ``/v1/embeddings`` (and recording what it was sent), a models file routing
three chat models (one with an alias), one hidden chat model and one embedding
model to it, and throwaway accounts with random passwords. No real provider,
account or shared database is involved: a pass proves the gateway's console
create path end to end -- session auth, upload, preflight, submission under the
account's API key, the worker running the batch -- not a provider's behaviour.

Subcommands (each with ``--help``):

``upstream``
    Serve the fake provider. A prompt containing ``SLOW`` waits two seconds,
    so a batch can be cancelled while it runs. ``GET /_calls`` lists the calls.
``models``
    Write the models file for the fake upstream.
``seed``
    Create the accounts through the gateway's own store code, in the gateway's
    database (run with its ``DB_*`` environment and ``API_KEY_SECRET``):
    ``owner`` (internal, with a key; ``smoke-hidden`` disabled in their
    preferences), ``stranger`` (internal, with a key), ``admin`` (admin, with a
    key) and ``nokey`` (internal, no key). Prints JSON credentials.
``check``
    The acceptance checks over HTTP with session tokens from ``/auth/login``.

A typical local run, every port and path your own::

    python ops/dev/batch_console_smoke.py upstream --port 18190 &
    python ops/dev/batch_console_smoke.py models --upstream http://127.0.0.1:18190 --out /tmp/m.yaml
    # start the gateway with MODELS_CONFIG_PATH=/tmp/m.yaml and its own database
    python ops/dev/batch_console_smoke.py seed > /tmp/accounts.json
    python ops/dev/batch_console_smoke.py check --gateway http://127.0.0.1:18191 \\
        --upstream http://127.0.0.1:18190 --accounts /tmp/accounts.json

Exit status 0 and a final ``BATCH_CONSOLE_SMOKE_OK`` line mean every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any

CHAT = "smoke-chat"
CHAT_ALIAS = "smoke-chat-alias"
CHAT_B = "smoke-chat-b"
HIDDEN = "smoke-hidden"
#: Declares no sampling parameter (``supported_params: []``).
BARE = "smoke-bare"
EMBED = "smoke-embed"
OK_MARKER = "BATCH_CONSOLE_SMOKE_OK"
SECRET = "prompt-text-never-echoed"


class SmokeError(AssertionError):
    """One acceptance check failed."""


def _expect(condition: bool, what: str) -> None:
    if not condition:
        raise SmokeError(what)
    print(f"PASS {what}", flush=True)


# -- fake upstream -------------------------------------------------------------------


def _serve_upstream(port: int) -> None:
    from aiohttp import web

    calls: list[dict[str, Any]] = []

    async def chat(request: web.Request) -> web.Response:
        body = await request.json()
        content = body["messages"][-1]["content"]
        text = content if isinstance(content, str) else json.dumps(content)
        calls.append({"path": "chat", "body": body})
        if "SLOW" in text:
            await asyncio.sleep(2)
        return web.json_response(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )

    async def embeddings(request: web.Request) -> web.Response:
        body = await request.json()
        calls.append({"path": "embeddings", "body": body})
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {"object": "embedding", "index": index, "embedding": [0.1, 0.2, 0.3]}
                    for index, _ in enumerate(inputs)
                ],
                "model": body.get("model"),
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            }
        )

    async def list_calls(_request: web.Request) -> web.Response:
        return web.json_response(calls)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_post("/v1/embeddings", embeddings)
    app.router.add_get("/_calls", list_calls)
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
    route = (
        "    route:\n"
        "      - kind: openai_compat\n"
        "        weight: 1.0\n"
        f"        base_url: {base}/v1\n"
        "        api_key: smoke-not-a-real-key\n"
    )
    chat_models = "".join(
        f"  - id: {model_id}\n    name: {name}\n    provider: openai_compat\n{extra}{common}{route}"
        for model_id, name, extra in (
            (CHAT, "Smoke Chat", f"    aliases: [{CHAT_ALIAS}]\n"),
            (CHAT_B, "Smoke Chat B", ""),
            (HIDDEN, "Smoke Hidden", ""),
            (BARE, "Smoke Bare", "    supported_params: []\n"),
        )
    )
    out.write_text(
        "models:\n"
        f"{chat_models}"
        f"  - id: {EMBED}\n"
        "    name: Smoke Embeddings\n"
        "    provider: openai_compat\n"
        "    model_type: embedding\n"
        "    required_role: free\n"
        "    route:\n"
        "      - kind: openai_compat\n"
        f"        base_url: {base}/v1\n"
        "        api_key: smoke-not-a-real-key\n"
    )
    print(out)


async def _seed() -> dict[str, Any]:
    import os
    from decimal import Decimal

    import asyncpg

    from serving.servers.auth import generate_api_key, hash_api_key
    from serving.storage.postgres_operational import PostgresOperationalStore
    from serving.utils.password import hash_password

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
        if not os.environ.get("API_KEY_SECRET"):
            # A gateway that generated its own secret on first start keeps it in
            # its database; the keys made here must hash the same way.
            secret = await pool.fetchval(
                "SELECT value FROM app_config WHERE key = 'API_KEY_SECRET'"
            )
            if secret:
                os.environ["API_KEY_SECRET"] = secret
                from serving.config.settings import get_settings

                get_settings.cache_clear()
        store = PostgresOperationalStore(pool)
        accounts: dict[str, Any] = {}
        for name, role, with_key in (
            ("owner", "internal", True),
            ("stranger", "internal", True),
            ("admin", "admin", True),
            ("nokey", "internal", False),
        ):
            tag = uuid.uuid4().hex[:8]
            user_id = f"smoke-{name}-{tag}"
            email = f"{name}-{tag}@smoke.example.com"
            password = secrets.token_urlsafe(18)
            await store.create_user(
                user_id=user_id,
                email=email,
                password_hash=hash_password(password),
                email_verified=True,
            )
            await store.update_user_fields(user_id, role=role)
            account = {"user_id": user_id, "email": email, "password": password}
            if with_key:
                key = generate_api_key()
                await store.create_key(
                    key_hash=hash_api_key(key),
                    key_prefix=key[:12],
                    user_id=user_id,
                    account_id=user_id,
                    quota_daily_cost_usd=Decimal("100.00"),
                )
                account["api_key"] = key
            accounts[name] = account
        await store.set_user_preference(accounts["owner"]["user_id"], "disabled_models", [HIDDEN])
        return accounts
    finally:
        await pool.close()


# -- the client side -------------------------------------------------------------------


class Console:
    """The gateway as one signed-in console user sees it (session token, no cookie)."""

    def __init__(self, base: str, account: dict[str, Any]) -> None:
        import httpx

        self.base = base.rstrip("/")
        self.account = account
        self.http = httpx.Client(base_url=self.base, timeout=120)
        response = self.http.post(
            "/auth/login", json={"email": account["email"], "password": account["password"]}
        )
        if response.status_code != 200:
            raise SmokeError(f"login {account['email']}: {response.status_code} {response.text}")
        self.headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        # Only the bearer token: the refresh cookie must never authorise these routes.
        self.http.cookies.clear()

    def get(self, path: str, **kwargs: Any) -> Any:
        """GET as this user."""
        return self.http.get(path, headers=self.headers, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        """POST as this user (extra ``headers`` are merged in)."""
        headers = {**self.headers, **kwargs.pop("headers", {})}
        return self.http.post(path, headers=headers, **kwargs)

    def delete(self, path: str) -> Any:
        """DELETE as this user."""
        return self.http.delete(path, headers=self.headers)

    def upload(self, payload: bytes, filename: str = "requests.jsonl") -> dict[str, Any]:
        """Upload a source file; return its file object."""
        response = self.post(
            "/user/batch-files",
            files={"file": (filename, payload, "application/jsonl")},
            data={"purpose": "batch"},
        )
        if response.status_code != 200:
            raise SmokeError(f"upload: {response.status_code} {response.text}")
        return response.json()

    def preflight(self, file_id: str, phase: str = "validate", **extra: Any) -> dict[str, Any]:
        """Preflight a source file; return the report."""
        response = self.post(
            "/user/batch-preflight", json={"source_file_id": file_id, "phase": phase, **extra}
        )
        if response.status_code != 200:
            raise SmokeError(f"preflight: {response.status_code} {response.text}")
        return response.json()

    def submission(self, source: dict[str, Any], report: dict[str, Any], **extra: Any) -> dict:
        """The body of a confirmed submission, from a report and the current context."""
        context = self.get("/user/batch-create/context").json()
        credential = context["credential"] or {"key_id": "0", "revision": "none"}
        body = {
            "source_file_id": source["id"],
            "source_sha256": source["sha256"],
            "endpoint": report["detected"]["endpoint"],
            "overrides": {},
            "confirmed_model": report["model_resolution"],
            "credential": {"key_id": credential["key_id"], "revision": credential["revision"]},
            "name": f"smoke-{uuid.uuid4().hex[:6]}",
            "description": "",
            "copied_from": None,
        }
        body.update(extra)
        return body

    def submit(self, body: dict[str, Any], key: str | None = None) -> Any:
        """Submit under ``key`` (a new one by default)."""
        return self.post(
            "/user/batches",
            json=body,
            headers={"Idempotency-Key": key or f"console-{uuid.uuid4()}"},
        )

    def wait(self, batch_id: str, wanted: str, timeout: float = 90) -> dict[str, Any]:
        """Wait until a batch shows ``wanted`` with its results available."""
        deadline = time.monotonic() + timeout
        while True:
            row = self.get(f"/user/batches/{batch_id}").json()
            if row.get("status") == wanted and row.get("results_available"):
                return row
            if time.monotonic() > deadline:
                raise SmokeError(f"{batch_id} did not reach {wanted}: {row.get('status')}")
            time.sleep(0.5)


def _jsonl(rows: list[Any]) -> bytes:
    return ("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n").encode()


def _chat(custom_id: str, model: str, *, url: bool = True, text: str = "", **body: Any) -> dict:
    row: dict[str, Any] = {
        "custom_id": custom_id,
        "body": {
            "model": model,
            "messages": [{"role": "user", "content": f"{SECRET} {text or custom_id}"}],
            **body,
        },
    }
    if url:
        row["url"] = "/v1/chat/completions"
    return row


def _code(response: Any) -> str | None:
    try:
        return (response.json().get("error") or {}).get("code")
    except ValueError:
        return None


def _calls(upstream: str) -> list[dict[str, Any]]:
    import httpx

    return httpx.get(f"{upstream.rstrip('/')}/_calls", timeout=10).json()


def _check(base: str, upstream: str, accounts: dict[str, Any]) -> None:
    import httpx

    owner = Console(base, accounts["owner"])
    stranger = Console(base, accounts["stranger"])
    admin = Console(base, accounts["admin"])
    nokey = Console(base, accounts["nokey"])

    # Cookie only, no Authorization header: never a session.
    cookie_only = httpx.post(
        f"{base}/user/batches", json={}, cookies={"refresh_token": "x"}, timeout=10
    )
    _expect(cookie_only.status_code == 401, "a cookie alone is not a console session (401)")

    context = owner.get("/user/batch-create/context").json()
    _expect(
        context["credential"]["key_prefix"] == accounts["owner"]["api_key"][:12],
        "the context names the owner's key by its prefix",
    )
    _expect(accounts["owner"]["api_key"] not in json.dumps(context), "the key itself never leaves")
    chat_ids = {m["id"] for m in context["models"]["chat"]}
    _expect(CHAT in chat_ids and HIDDEN not in chat_ids, "a disabled model is not offered")
    _expect(EMBED in {m["id"] for m in context["models"]["embeddings"]}, "embedding models listed")

    # A1/A24: a Chat file with urls, run as is from its own bytes.
    calls_before = len(_calls(upstream))
    source = owner.upload(_jsonl([_chat(f"q-{i}", CHAT, temperature=0) for i in range(4)]))
    identified = owner.preflight(source["id"], phase="identify")
    _expect(
        identified["detected"]["type"] == "chat" and identified["detected"]["source"] == "url",
        "the type is recognised from url",
    )
    report = owner.preflight(source["id"])
    _expect(report["valid"] and not report["stores_copy"], "valid, and stored as uploaded")
    _expect(len(_calls(upstream)) == calls_before, "a preflight calls no model")
    created = owner.submit(owner.submission(source, report, name="smoke-a1"))
    _expect(created.status_code == 200, f"submitted ({created.status_code})")
    batch = created.json()
    _expect(batch["input_file_id"] == source["id"], "the batch reads the source itself")
    done = owner.wait(batch["id"], "completed")
    _expect(done["request_counts"] == {"total": 4, "completed": 4, "failed": 0}, "it completes")
    listed = owner.get("/user/batches", params={"q": "smoke-a1"}).json()["data"]
    _expect([row["id"] for row in listed] == [batch["id"]], "found by name")
    listed = owner.get("/user/batches", params={"status": "completed", "q": batch["id"]}).json()
    _expect(len(listed["data"]) == 1, "found by id and the status it shows")

    # A2: a copy with another model and temperature, through a stored copy.
    overrides = {"model": CHAT_B, "temperature": 0.2}
    report = owner.preflight(source["id"], overrides=overrides)
    _expect(report["valid"] and report["stores_copy"], "the override is checked and stored apart")
    calls_before = len(_calls(upstream))
    copy = owner.submit(
        owner.submission(
            source, report, overrides=overrides, copied_from=batch["id"], name="smoke-a2"
        )
    )
    copy_batch = copy.json()
    _expect(copy_batch["input_file_id"] != source["id"], "the copy reads its own stored requests")
    owner.wait(copy_batch["id"], "completed")
    sent = [c["body"] for c in _calls(upstream)[calls_before:] if c["path"] == "chat"]
    _expect(
        len(sent) == 4 and all(b["model"] == CHAT_B and b["temperature"] == 0.2 for b in sent),
        "every request ran on the chosen model with the chosen temperature",
    )

    # A24: embeddings without urls, by their shape.
    emb = owner.upload(
        _jsonl(
            [{"custom_id": f"e-{i}", "body": {"model": EMBED, "input": f"t{i}"}} for i in range(2)]
        )
    )
    report = owner.preflight(emb["id"])
    _expect(
        report["detected"]["type"] == "embeddings" and report["detected"]["source"] == "structure",
        "embeddings recognised from their shape",
    )
    emb_batch = owner.submit(owner.submission(emb, report)).json()
    owner.wait(emb_batch["id"], "completed")

    # A number past Python's integer conversion limit is a row error, not a 500.
    big = owner.upload(
        _jsonl(
            [
                json.dumps(_chat("big", CHAT)).replace(
                    '"messages"', '"max_tokens": ' + "9" * 5000 + ', "messages"', 1
                )
            ]
        )
    )
    for phase in ("identify", "validate"):
        response = owner.post(
            "/user/batch-preflight", json={"source_file_id": big["id"], "phase": phase}
        )
        _expect(
            response.status_code == 200
            and [e["code"] for e in response.json()["errors"]] == ["invalid_json_line"],
            f"a 5000-digit integer is a row error ({phase})",
        )

    # A model declaring no sampling parameter takes no temperature override.
    report = owner.preflight(source["id"], overrides={"model": BARE, "temperature": 0.7})
    _expect(
        [(e["code"], e["field"]) for e in report["errors"]]
        == [("unsupported_parameter", "overrides.temperature")],
        "an explicitly empty capability list refuses temperature",
    )

    # A6/A9: problems by line, no prompt echoed, nothing created.
    bad = owner.upload(
        _jsonl(
            [
                _chat("ok", CHAT),
                _chat("ok", CHAT),
                _chat("mct", CHAT, max_completion_tokens=5),
                '{"custom_id":"x","body":{"model":"m","model":"m"}}',
                "not json " + SECRET,
            ]
        )
    )
    report = owner.preflight(bad["id"])
    codes = sorted(error["code"] for error in report["errors"])
    _expect(
        codes
        == ["duplicate_custom_id", "duplicate_key", "invalid_json_line", "unsupported_parameter"],
        f"every problem reported ({codes})",
    )
    _expect(SECRET not in json.dumps(report["errors"]), "no request content in the errors")

    # A25: a choice only when the file says nothing; never against the file.
    vague = owner.upload(_jsonl([{"custom_id": "v", "body": {"model": CHAT}}]))
    report = owner.preflight(vague["id"], phase="identify")
    _expect(report["detected"]["needs_type_choice"], "an undetermined file asks for the type")
    mixed = owner.upload(
        _jsonl(
            [
                _chat("c", CHAT),
                {"custom_id": "e", "url": "/v1/embeddings", "body": {"model": EMBED, "input": "x"}},
            ]
        )
    )
    report = owner.preflight(mixed["id"], endpoint="/v1/chat/completions")
    _expect(
        not report["valid"] and report["detected"]["type"] is None,
        "a mixed file is refused, whatever is chosen",
    )

    # Finding 3: the account's disabled model is refused like an unknown one.
    hidden = owner.upload(_jsonl([_chat("h", HIDDEN)]))
    report = owner.preflight(hidden["id"])
    _expect(
        [e["code"] for e in report["errors"]] == ["model_not_found"],
        "a disabled model is refused without a key or quota",
    )

    # A12: no key; a changed credential.
    nk = nokey.upload(_jsonl([_chat("n", CHAT)]))
    nk_report = nokey.preflight(nk["id"])
    _expect(nk_report["valid"], "an account without a key can still check a file")
    refused = nokey.submit(nokey.submission(nk, nk_report))
    _expect(
        refused.status_code == 409 and _code(refused) == "no_worker_credential",
        "no key: 409 no_worker_credential",
    )
    stale = owner.submission(source, owner.preflight(source["id"]))
    stale["credential"]["revision"] = "0" * 24
    refused = owner.submit(stale)
    _expect(
        refused.status_code == 409 and _code(refused) == "credential_changed",
        "a credential other than the active one: 409 credential_changed",
    )

    # A11: the confirmed model no longer what the choice resolves to.
    alias = owner.upload(_jsonl([_chat("al", CHAT_ALIAS)]))
    report = owner.preflight(alias["id"])
    _expect(report["model_resolution"]["canonical"] == CHAT, "an alias resolves to its model")
    wrong = owner.submission(
        alias, report, confirmed_model={"requested": CHAT, "canonical": CHAT_B}
    )
    refused = owner.submit(wrong)
    _expect(
        refused.status_code == 409 and _code(refused) == "model_resolution_changed",
        "a changed resolution: 409 model_resolution_changed",
    )

    # A15/A16: one key, one batch; another key, another batch; deletion.
    body = owner.submission(alias, report, name="smoke-a15")
    key = f"console-{uuid.uuid4()}"
    first, second = owner.submit(body, key), owner.submit(body, key)
    _expect(
        first.status_code == second.status_code == 200
        and first.json()["id"] == second.json()["id"],
        "the same key replays its batch",
    )
    conflict = owner.submit({**body, "name": "other"}, key)
    _expect(conflict.status_code == 409, "the same key with other content: 409")
    other = owner.submit(body)
    _expect(other.json()["id"] != first.json()["id"], "a new key is a new batch")
    found = owner.get(f"/user/batch-submissions/{key}").json()
    _expect(
        found["state"] == "created" and found["batch"]["id"] == first.json()["id"],
        "a submission key is looked up without creating anything",
    )
    owner.wait(first.json()["id"], "completed")
    owner.delete(f"/user/batches/{first.json()['id']}")
    gone = owner.submit(body, key)
    _expect(gone.status_code == 410, "after deletion the key answers 410, not a new batch")

    # A17: cancel while running; export the unfinished requests.
    # More slow requests than the role's batch concurrency (64 by default): some
    # are still queued when the batch is cancelled.
    slow = owner.upload(_jsonl([_chat(f"s-{i}", CHAT, text="SLOW") for i in range(150)]))
    report = owner.preflight(slow["id"])
    running = owner.submit(owner.submission(slow, report)).json()
    deadline = time.monotonic() + 60
    while owner.get(f"/user/batches/{running['id']}").json()["status"] != "in_progress":
        if time.monotonic() > deadline:
            raise SmokeError("the slow batch never started")
        time.sleep(0.2)
    owner.post(f"/user/batches/{running['id']}/cancel")
    cancelled = owner.wait(running["id"], "cancelled")
    exported = owner.get(f"/user/batches/{running['id']}/unfinished-requests")
    lines = [json.loads(line) for line in exported.text.splitlines()]
    _expect(
        exported.status_code == 200 and len(lines) == cancelled["cancelled"] > 0,
        f"the cancelled requests are exported ({len(lines)})",
    )

    # A19 / finding 5: the stranger and an administrator find nothing of the owner's.
    for intruder, who in ((stranger, "another user"), (admin, "an administrator")):
        statuses = {
            intruder.get(f"/user/batches/{batch['id']}").status_code,
            intruder.get(f"/user/batches/{batch['id']}/requests").status_code,
            intruder.get(f"/user/batch-files/{source['id']}/content").status_code,
            intruder.delete(f"/user/batch-files/{source['id']}").status_code,
        }
        _expect(statuses == {404}, f"{who} gets 404 for the owner's batch and files")
        _expect(
            intruder.get(f"/user/batch-submissions/{key}").json()["state"] == "unknown",
            f"{who} cannot look up the owner's submission",
        )

    # Finding 6: the public API never starts a batch from a derived copy.
    refused = httpx.post(
        f"{base}/v1/batches",
        headers={"Authorization": f"Bearer {accounts['owner']['api_key']}"},
        json={
            "input_file_id": copy_batch["input_file_id"],
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        },
        timeout=30,
    )
    _expect(refused.status_code == 400, "a derived copy cannot start an API batch")

    # A14: the source deleted; a copy then asks for the file again.
    _expect(owner.delete(f"/user/batch-files/{source['id']}").status_code == 200, "source deleted")
    detail = owner.get(f"/user/batches/{batch['id']}").json()
    _expect(detail["copy"]["state"] == "reupload", "a copy of it now asks to upload the file again")
    old = owner.submission(
        source,
        {
            "detected": {"endpoint": "/v1/chat/completions"},
            "model_resolution": {"requested": CHAT, "canonical": CHAT},
        },
    )
    refused = owner.submit(old)
    _expect(
        refused.status_code == 409 and _code(refused) == "source_unavailable",
        "submitting from a deleted source: 409 source_unavailable",
    )
    copy_detail = owner.get(f"/user/batches/{copy_batch['id']}").json()
    _expect(copy_detail["status"] == "completed", "the batch already created from it is unaffected")


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("upstream", help="serve the fake provider")
    up.add_argument("--port", type=int, required=True)
    models = sub.add_parser("models", help="write the models file")
    models.add_argument("--upstream", required=True)
    models.add_argument("--out", type=Path, required=True)
    sub.add_parser("seed", help="create the synthetic accounts; prints JSON")
    check = sub.add_parser("check", help="run the acceptance checks")
    check.add_argument("--gateway", required=True)
    check.add_argument("--upstream", required=True)
    check.add_argument("--accounts", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "upstream":
        _serve_upstream(args.port)
    elif args.command == "models":
        _write_models(args.upstream, args.out)
    elif args.command == "seed":
        print(json.dumps(asyncio.run(_seed())))
    else:
        try:
            _check(args.gateway, args.upstream, json.loads(args.accounts.read_text()))
        except SmokeError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1
        print(OK_MARKER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
