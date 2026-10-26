"""Auto models on the inference surfaces (``serving/auto_mode.py``).

A request naming ``auto-<tier>`` is rewritten to the model the tier chooses and
then served as a request naming that model. The caller is never told which: the
response, its stream and its errors name the auto model. Which model served,
and why, is what the log records, so that is what these tests read.

They drive the real handlers and router: the candidates are three models with
different windows and capabilities, a provider is taken down through the admin
kill switch, and the switch that turns auto on is the runtime setting itself.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import serving.config.runtime_settings as runtime_settings_module
import serving.servers.concurrency as concurrency_module
from routing.model_selection import parse_auto_config
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.auto_mode import AUTO_MODE_SETTING_KEY, AutoMode
from serving.batch.interactive_load import InteractiveLoad
from serving.config.runtime_settings import RuntimeSettings
from serving.servers.auth import optional_verify_api_key, verify_api_key
from serving.servers.concurrency import UserConcurrencyLimiter, static_limits_provider
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import anthropic_messages, compat, completions, models, responses

from .test_completions import VisibilityResolver

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

FRONTIER = "frontier-a"
MID = "mid-b"
SMALL = "small-c"

AUTO_SECTION = {
    "default_profile": "balance",
    "profiles": {
        "intelligence": {
            "candidates": [
                {"model": FRONTIER, "reasoning_effort": "high"},
                {"model": MID, "reasoning_effort": "high"},
            ]
        },
        "balance": {"candidates": [{"model": MID}, {"model": FRONTIER}]},
        "fast": {"candidates": [{"model": SMALL}, {"model": MID, "reasoning_effort": "low"}]},
    },
}

USER = {"user_id": "user-1", "role": "free", "authenticated": True, "is_admin": False}
SESSION = {"X-Session-ID": "session-1"}


class _Recorder(BaseAdapter):
    """Answers as its own model and records what it was sent."""

    def __init__(self, config: ModelConfig, served: list[str]) -> None:
        super().__init__(config)
        self.calls: list[dict[str, Any]] = []
        self._served = served

    def _record(self, messages: list[dict[str, Any]], params: dict[str, Any]) -> None:
        self.calls.append({"messages": messages, "params": params})
        self._served.append(self.config.id)

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        self._record(messages, params)
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        self._record(messages, params)
        yield self.format_stream_chunk(model=self.config.id, content="ok")
        yield "data: [DONE]\n\n"


class _NativeRecorder(_Recorder):
    """An Anthropic-native endpoint: forwarded the body as it is."""

    native_format = "anthropic"

    async def messages(self, body, *, request_id, extra_headers=None) -> dict[str, Any]:
        self._record(copy.deepcopy(body["messages"]), {})
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": self.config.id,
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


def _cfg(model_id: str, **overrides) -> ModelConfig:
    return ModelConfig(
        **{
            "id": model_id,
            "name": model_id,
            "provider": f"provider-{model_id}",
            "base_url": "http://test",
            "supports_tools": True,
            "supported_params": ["temperature", "max_tokens", "reasoning_effort"],
            "reasoning_efforts": ["low", "high"],
            **overrides,
        }
    )


class _Disabled:
    """The admin provider kill switch, as the router reads it."""

    def __init__(self) -> None:
        self.providers: set[str] = set()

    def is_disabled(self, provider: str) -> bool:
        return provider in self.providers


class _SettingsStore:
    def __init__(self, values: dict[str, bool]) -> None:
        self.values = values

    async def get_setting(self, key: str) -> dict[str, str] | None:
        if key not in self.values:
            return None
        return {"value": str(self.values[key]), "value_type": "bool"}


class _Gateway:
    """The app, its candidates' adapters, and the controls a test turns."""

    def __init__(
        self,
        mock_db_logger,
        mock_log_store,
        *,
        user: dict[str, Any] = USER,
        native_frontier: bool = False,
    ):
        self._served: list[str] = []
        frontier_type = _NativeRecorder if native_frontier else _Recorder
        self.adapters = {
            FRONTIER: frontier_type(
                _cfg(
                    FRONTIER,
                    context_length=200_000,
                    max_output_length=32_000,
                    input_modalities=["text", "image"],
                ),
                self._served,
            ),
            MID: _Recorder(_cfg(MID, context_length=32_000, max_output_length=8_000), self._served),
            SMALL: _Recorder(
                _cfg(
                    SMALL,
                    context_length=16_000,
                    max_output_length=4_000,
                    supports_tools=False,
                    supported_params=["temperature", "max_tokens"],
                    reasoning_efforts=[],
                ),
                self._served,
            ),
        }
        self.router = FixedRouter()
        for model_id, adapter in self.adapters.items():
            self.router.register_route(model_id, [(adapter, 1.0)])
        self.disabled = _Disabled()
        self.router.disabled_provider_resolver = self.disabled
        self.visibility = VisibilityResolver({})
        self.log_store = mock_log_store
        self.app = FastAPI()
        self.app.state.services = AppServices(
            router=self.router,
            db_logger=mock_db_logger,
            log_store=mock_log_store,
            model_visibility_resolver=self.visibility,
            auto_mode=AutoMode(parse_auto_config(AUTO_SECTION)),
        )
        install_error_handlers(self.app)
        self.app.dependency_overrides[verify_api_key] = lambda: user
        self.app.dependency_overrides[optional_verify_api_key] = lambda: user
        for module in (completions, compat, anthropic_messages, models, responses):
            self.app.include_router(module.router)
        self.switch(on=True)

    def switch(self, *, on: bool) -> None:
        runtime_settings_module._runtime_settings = RuntimeSettings(
            _SettingsStore({AUTO_MODE_SETTING_KEY: on})
        )

    def down(self, *model_ids: str) -> None:
        self.disabled.providers = {f"provider-{model_id}" for model_id in model_ids}

    def served(self) -> list[str]:
        """Return which candidate served each call so far, in order."""
        return list(self._served)

    def last_call(self, model_id: str) -> dict[str, Any]:
        return self.adapters[model_id].calls[-1]

    async def post(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None):
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=body, headers=headers or {})

    async def get(self, path: str, headers: dict[str, str] | None = None):
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, headers=headers or {})

    async def logged(self, timeout: float = 2.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.log_store.log_request.call_count > 0:
                return self.log_store.log_request.call_args.kwargs
            await asyncio.sleep(0.02)
        raise AssertionError("no request was logged")

    async def sessions(self, count: int, timeout: float = 2.0) -> list[tuple[str, str]]:
        """Return ``(session action, model logged)`` for the first ``count`` messages requests.

        The log is written in the background; /v1/messages request ids order by
        arrival, so the rows are put back in the order the requests were sent.
        """
        deadline = time.time() + timeout
        while self.log_store.log_request.call_count < count:
            if time.time() > deadline:
                raise AssertionError(f"{self.log_store.log_request.call_count} of {count} logged")
            await asyncio.sleep(0.02)
        rows = sorted(
            (call.kwargs for call in self.log_store.log_request.call_args_list),
            key=lambda row: row["request_id"],
        )
        return [(row["metadata"]["auto"]["session_action"], row["model_id"]) for row in rows]


def _no_auto_headers(response) -> bool:
    return not any(name.lower().startswith("x-auto") for name in response.headers)


@pytest.fixture
def gateway(mock_db_logger, mock_log_store) -> _Gateway:
    return _Gateway(mock_db_logger, mock_log_store)


def _capture_interactive_load(gateway: _Gateway, monkeypatch) -> InteractiveLoad:
    load = InteractiveLoad()
    monkeypatch.setattr(concurrency_module, "INTERACTIVE_LOAD", load)
    gateway.app.state.services.user_concurrency_limiter = UserConcurrencyLimiter(
        static_limits_provider({"free": 10, "pro": 10, "internal": 10, "admin": 10})
    )
    return load


def _chat(model: str, messages: list[dict[str, Any]] | None = None, **extra) -> dict[str, Any]:
    return {"model": model, "messages": messages or [{"role": "user", "content": "hi"}], **extra}


def _anthropic(model: str, messages: list[dict[str, Any]] | None = None, **extra):
    return {
        "model": model,
        "max_tokens": 1024,
        "messages": messages or [{"role": "user", "content": "hi"}],
        **extra,
    }


OPENING = [{"role": "user", "content": "list the files"}]


def _tool_loop() -> list[dict[str, Any]]:
    """The conversation ``OPENING`` began, now returning a tool's result."""
    return [
        *OPENING,
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "ls", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py"}],
        },
    ]


# -- Turned off -------------------------------------------------------------------


_SURFACES = (
    pytest.param("/v1/chat/completions", _chat, id="chat-completions"),
    pytest.param("/v1/messages", _anthropic, id="anthropic-messages"),
    pytest.param(
        "/v1/messages/count_tokens",
        lambda model: {"model": model, "messages": [{"role": "user", "content": "hi"}]},
        id="count-tokens",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body"), _SURFACES)
async def test_with_the_switch_off_an_auto_model_does_not_exist(gateway, path, body):
    gateway.switch(on=False)

    response = await gateway.post(path, body("auto-balance"))

    assert response.status_code == 404
    assert gateway.served() == []


@pytest.mark.asyncio
async def test_with_the_switch_off_the_model_list_has_no_auto_model(gateway):
    gateway.switch(on=False)

    listed = {item["id"] for item in (await gateway.get("/v1/models")).json()["data"]}

    assert listed == {FRONTIER, MID, SMALL}


@pytest.mark.asyncio
async def test_a_request_naming_a_model_is_untouched(gateway):
    response = await gateway.post("/v1/chat/completions", _chat(MID))

    assert response.status_code == 200
    assert response.json()["model"] == MID
    logged = await gateway.logged()
    assert logged["model_id"] == MID
    assert "auto" not in logged["metadata"]


# -- Chat Completions -------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_is_served_by_the_first_choice_and_names_only_the_auto_model(gateway):
    response = await gateway.post("/v1/chat/completions", _chat("auto-balance"), SESSION)

    assert response.status_code == 200
    assert response.json()["model"] == "auto-balance"
    assert MID not in response.text
    assert _no_auto_headers(response)
    assert gateway.served() == [MID]
    logged = await gateway.logged()
    assert logged["model_id"] == MID
    assert logged["request_payload"]["model"] == "auto-balance"
    auto = logged["metadata"]["auto"]
    assert auto["requested"] == "auto-balance"
    assert auto["profile"] == "balance"
    assert auto["chosen"] == MID
    assert auto["ranked"] == [MID, FRONTIER]
    assert auto["session_action"] == "new"
    assert auto["session_source"] == "declared"
    assert auto["profile_version"]
    assert auto["scorer_version"] == "static-1"


@pytest.mark.asyncio
async def test_bare_auto_is_the_default_tier(gateway):
    response = await gateway.post("/v1/chat/completions", _chat("auto"))

    assert response.status_code == 200
    assert response.json()["model"] == "auto"
    assert (await gateway.logged())["metadata"]["auto"]["profile"] == "balance"


@pytest.mark.asyncio
async def test_every_chunk_of_a_streamed_chat_names_the_auto_model(gateway):
    response = await gateway.post("/v1/chat/completions", _chat("auto-balance", stream=True))

    assert response.status_code == 200
    assert gateway.served() == [MID]
    assert MID not in response.text
    assert response.text.count('"model": "auto-balance"') >= 2  # the role chunk and the reply
    assert _no_auto_headers(response)


@pytest.mark.asyncio
async def test_streamed_auto_chat_counts_on_the_concrete_model_until_eof(gateway, monkeypatch):
    load = _capture_interactive_load(gateway, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    adapter = gateway.adapters[MID]

    async def held_stream(messages, **params):
        adapter._record(messages, params)
        entered.set()
        yield adapter.format_stream_chunk(model=MID, content="ok")
        await release.wait()
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(adapter, "stream_chat_completion", held_stream)
    task = asyncio.create_task(
        gateway.post("/v1/chat/completions", _chat("auto-balance", stream=True))
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        assert load.in_flight(MID) == 1
        assert load.in_flight("auto-balance") == 0
    finally:
        release.set()

    assert (await task).status_code == 200
    assert load.in_flight(MID) == 0


@pytest.mark.asyncio
async def test_an_upstream_error_does_not_name_the_model(gateway):
    """An upstream's own message quotes its model; the caller sees the auto model instead."""
    import aiohttp
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    url = URL("https://upstream.example/v1/chat/completions")

    async def overloaded(messages, **params):
        raise aiohttp.ClientResponseError(
            request_info=aiohttp.RequestInfo(url, "POST", CIMultiDictProxy(CIMultiDict()), url),
            history=(),
            status=400,
            message=f"{MID} rejected the request: context too long for {MID}.",
        )

    gateway.adapters[MID].chat_completion = overloaded

    response = await gateway.post("/v1/chat/completions", _chat("auto-balance"))

    assert response.status_code == 400
    assert MID not in response.text
    assert "auto-balance rejected the request" in response.text


@pytest.mark.asyncio
async def test_the_candidates_effort_is_applied_unless_the_caller_chose_one(gateway):
    await gateway.post("/v1/chat/completions", _chat("auto-intelligence"))
    await gateway.post("/v1/chat/completions", _chat("auto-intelligence", reasoning_effort="low"))

    first, second = gateway.adapters[FRONTIER].calls
    assert first["params"]["reasoning_effort"] == "high"
    assert second["params"]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_a_model_the_caller_may_not_use_is_never_chosen(gateway):
    """The admin raised mid-b to admin-only: a free caller's balance goes to frontier-a."""
    gateway.visibility._overrides = {MID: "admin"}

    response = await gateway.post("/v1/chat/completions", _chat("auto-balance"))

    assert response.status_code == 200
    assert gateway.served() == [FRONTIER]
    auto = (await gateway.logged())["metadata"]["auto"]
    assert auto["dropped"] == {MID: "unavailable"}


@pytest.mark.asyncio
async def test_a_grant_reaches_only_the_candidates_it_names(mock_db_logger, mock_log_store):
    gateway = _Gateway(
        mock_db_logger, mock_log_store, user={**USER, "agent_allowed_models": [FRONTIER]}
    )

    response = await gateway.post("/v1/chat/completions", _chat("auto-balance"))

    assert response.status_code == 200
    assert gateway.served() == [FRONTIER]


@pytest.mark.asyncio
async def test_a_tier_the_caller_can_use_none_of_is_an_unknown_model(gateway):
    gateway.visibility._overrides = {SMALL: "admin", MID: "admin"}

    response = await gateway.post("/v1/chat/completions", _chat("auto-fast"))

    assert response.status_code == 404
    assert "auto-fast" in response.text
    assert SMALL not in response.text and MID not in response.text


@pytest.mark.asyncio
async def test_a_tier_whose_usable_models_are_all_down_is_unavailable(gateway):
    gateway.down(MID, FRONTIER)

    response = await gateway.post("/v1/chat/completions", _chat("auto-balance"))

    assert response.status_code == 503
    assert gateway.served() == []


@pytest.mark.asyncio
async def test_a_prompt_too_long_for_the_first_choice_goes_to_a_wider_window(gateway):
    long_prompt = "word " * 40_000  # ~50k tokens estimated, past mid-b's 32k window

    response = await gateway.post(
        "/v1/chat/completions", _chat("auto-balance", [{"role": "user", "content": long_prompt}])
    )

    assert response.status_code == 200
    assert gateway.served() == [FRONTIER]
    assert (await gateway.logged())["metadata"]["auto"]["dropped"] == {MID: "context"}


@pytest.mark.asyncio
async def test_a_request_no_usable_model_can_take_is_refused_as_such(gateway):
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    message = {"role": "user", "content": [{"type": "text", "text": "what is this"}, image]}

    response = await gateway.post("/v1/chat/completions", _chat("auto-fast", [message]))

    assert response.status_code == 400
    assert "image" in response.text
    assert gateway.served() == []


# -- Sessions ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_session_stays_on_its_model_in_a_tool_loop_and_moves_at_the_next_turn(gateway):
    """mid-b is down, so the session starts on frontier-a; mid-b recovers mid-loop."""
    gateway.down(MID)
    await gateway.post("/v1/messages", _anthropic("auto-balance", OPENING), SESSION)
    gateway.down()

    await gateway.post("/v1/messages", _anthropic("auto-balance", _tool_loop()), SESSION)
    await gateway.post(
        "/v1/messages",
        _anthropic(
            "auto-balance",
            [
                *_tool_loop(),
                {"role": "assistant", "content": [{"type": "text", "text": "a.py"}]},
                {"role": "user", "content": "now read it"},
            ],
        ),
        SESSION,
    )

    assert gateway.served() == [FRONTIER, FRONTIER, MID]
    assert await gateway.sessions(3) == [
        ("new", FRONTIER),
        ("keep", FRONTIER),
        ("turn_switch", MID),
    ]


@pytest.mark.asyncio
async def test_a_tool_loop_moves_at_once_when_its_model_goes_down(gateway):
    await gateway.post("/v1/messages", _anthropic("auto-balance", OPENING), SESSION)
    gateway.down(MID)

    response = await gateway.post("/v1/messages", _anthropic("auto-balance", _tool_loop()), SESSION)

    assert response.status_code == 200
    assert await gateway.sessions(2) == [("new", MID), ("forced_switch", FRONTIER)]


@pytest.mark.asyncio
async def test_a_conversation_in_the_same_session_keeps_its_own_model(gateway):
    """A subagent's conversation, under the session of the main one, is ranked as new.

    The main conversation fell back to frontier-a and is in its tool loop. A
    subagent starts after mid-b recovered and gets mid-b; the main loop stays.
    """
    gateway.down(MID)
    await gateway.post("/v1/messages", _anthropic("auto-balance", OPENING), SESSION)
    gateway.down()

    await gateway.post(
        "/v1/messages",
        _anthropic("auto-balance", [{"role": "user", "content": "search the docs"}]),
        SESSION,
    )
    await gateway.post("/v1/messages", _anthropic("auto-balance", _tool_loop()), SESSION)

    assert await gateway.sessions(3) == [
        ("new", FRONTIER),
        ("new", MID),
        ("keep", FRONTIER),
    ]


@pytest.mark.asyncio
async def test_sessions_are_kept_per_caller(mock_db_logger, mock_log_store):
    """Another caller declaring the same session id does not inherit its model."""
    gateway = _Gateway(mock_db_logger, mock_log_store)
    gateway.down(MID)
    await gateway.post("/v1/messages", _anthropic("auto-balance", OPENING), SESSION)
    gateway.down()
    other = {**USER, "user_id": "user-2"}
    gateway.app.dependency_overrides[verify_api_key] = lambda: other

    await gateway.post("/v1/messages", _anthropic("auto-balance", _tool_loop()), SESSION)

    assert await gateway.sessions(2) == [("new", FRONTIER), ("new", MID)]


# -- Anthropic Messages -----------------------------------------------------------


def _thinking(text: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": ""}


@pytest.mark.asyncio
async def test_an_anthropic_native_model_is_not_sent_another_models_thinking(
    mock_db_logger, mock_log_store
):
    """mid-b wrote thinking; the session is forced onto frontier-a, which checks signatures.

    Its own thinking from the loop's last step stays, as an upstream with
    thinking enabled requires it there.
    """
    gateway = _Gateway(mock_db_logger, mock_log_store, native_frontier=True)
    await gateway.post("/v1/messages", _anthropic("auto-balance", OPENING), SESSION)
    by_mid = {"role": "assistant", "content": [_thinking("mid"), _tool_loop()[1]["content"][0]]}
    result = _tool_loop()[2]
    gateway.down(MID)

    switched = await gateway.post(
        "/v1/messages", _anthropic("auto-balance", [*OPENING, by_mid, result]), SESSION
    )
    gateway.down()
    by_frontier = {
        "role": "assistant",
        "content": [_thinking("frontier"), {"type": "tool_use", "id": "t2", "name": "cat"}],
    }
    await gateway.post(
        "/v1/messages",
        _anthropic("auto-balance", [*OPENING, by_mid, result, by_frontier, result]),
        SESSION,
    )

    assert switched.json()["model"] == "auto-balance"
    assert await gateway.sessions(3) == [
        ("new", MID),
        ("forced_switch", FRONTIER),
        ("keep", FRONTIER),
    ]
    first, second = gateway.adapters[FRONTIER].calls
    assert [b["type"] for b in first["messages"][1]["content"]] == ["tool_use"]
    assert [b["type"] for b in second["messages"][1]["content"]] == ["tool_use"]
    assert [b["type"] for b in second["messages"][3]["content"]] == ["thinking", "tool_use"]
    prompts = [call.kwargs["prompt"] for call in gateway.log_store.log_request.call_args_list]
    assert all(p[1]["content"][0] == _thinking("mid") for p in prompts if len(p) > 1)


@pytest.mark.asyncio
async def test_messages_is_served_and_logged_under_the_chosen_model(gateway):
    response = await gateway.post("/v1/messages", _anthropic("auto-intelligence"), SESSION)

    assert response.status_code == 200
    assert response.json()["model"] == "auto-intelligence"
    assert _no_auto_headers(response)
    assert gateway.served() == [FRONTIER]
    assert gateway.last_call(FRONTIER)["params"]["reasoning_effort"] == "high"
    logged = await gateway.logged()
    assert logged["model_id"] == FRONTIER
    assert logged["request_payload"]["model"] == "auto-intelligence"
    assert "output_config" not in logged["request_payload"]
    assert logged["metadata"]["alias_input"] is None
    assert logged["metadata"]["auto"]["effort_applied"] == "high"


@pytest.mark.asyncio
async def test_a_streamed_message_names_the_auto_model(gateway):
    response = await gateway.post("/v1/messages", _anthropic("auto-balance", stream=True))

    assert response.status_code == 200
    assert gateway.served() == [MID]
    assert "event: message_start" in response.text
    assert '"model": "auto-balance"' in response.text
    assert MID not in response.text
    assert _no_auto_headers(response)


@pytest.mark.asyncio
async def test_messages_with_tools_skips_a_model_that_would_drop_them(gateway):
    tools = [{"name": "ls", "description": "list", "input_schema": {"type": "object"}}]

    response = await gateway.post("/v1/messages", _anthropic("auto-fast", tools=tools))

    assert response.status_code == 200
    assert gateway.served() == [MID]
    assert gateway.last_call(MID)["params"]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_count_tokens_answers_for_an_auto_model(gateway):
    body = {"model": "auto-balance", "messages": [{"role": "user", "content": "hi"}]}

    response = await gateway.post("/v1/messages/count_tokens", body)

    assert response.status_code == 200
    assert response.json()["input_tokens"] > 0
    assert gateway.served() == []


@pytest.mark.asyncio
async def test_count_tokens_for_a_tier_the_caller_can_use_none_of_is_404(gateway):
    gateway.visibility._overrides = {SMALL: "admin", MID: "admin"}
    body = {"model": "auto-fast", "messages": [{"role": "user", "content": "hi"}]}

    response = await gateway.post("/v1/messages/count_tokens", body)

    assert response.status_code == 404


# -- Responses API and the model list ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["whole", "streamed"])
async def test_the_responses_api_names_the_auto_model(gateway, stream):
    response = await gateway.post(
        "/v1/responses", {"model": "auto-balance", "input": "hi", "stream": stream}
    )

    assert response.status_code == 200
    assert gateway.served() == [MID]
    assert "auto-balance" in response.text
    assert MID not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param(
            "/v1/responses",
            {"model": "auto-balance", "input": "hi"},
            id="responses-direct-delegate",
        ),
        pytest.param("/completion", _chat("auto-balance"), id="legacy-direct-delegate"),
        pytest.param(
            "/v1/completions",
            {"model": "auto-balance", "prompt": "hi"},
            id="openai-legacy-completions",
        ),
        pytest.param("/v1/messages", _anthropic("auto-balance"), id="anthropic"),
    ],
)
async def test_unary_auto_surfaces_retarget_concrete_load(gateway, monkeypatch, path, body):
    load = _capture_interactive_load(gateway, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    adapter = gateway.adapters[MID]
    original = adapter.chat_completion

    async def held_completion(messages, **params):
        entered.set()
        await release.wait()
        return await original(messages, **params)

    monkeypatch.setattr(adapter, "chat_completion", held_completion)
    task = asyncio.create_task(gateway.post(path, body))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        assert load.in_flight(MID) == 1
        assert load.in_flight("auto-balance") == 0
    finally:
        release.set()

    assert (await task).status_code == 200
    assert load.in_flight(MID) == 0


@pytest.mark.asyncio
async def test_the_model_list_offers_each_tier_by_what_its_models_offer(gateway):
    items = {item["id"]: item for item in (await gateway.get("/v1/models")).json()["data"]}

    assert {"auto", "auto-intelligence", "auto-balance", "auto-fast"} <= set(items)
    balance = items["auto-balance"]
    assert balance["context_length"] == 200_000
    assert balance["input_modalities"] == ["image", "text"]
    assert "tools" in balance["supported_features"]
    assert balance["owned_by"] == "auto"


@pytest.mark.asyncio
async def test_the_model_list_leaves_out_a_tier_the_caller_can_use_none_of(gateway):
    gateway.visibility._overrides = {SMALL: "admin", MID: "admin"}

    listed = {item["id"] for item in (await gateway.get("/v1/models")).json()["data"]}

    assert "auto-fast" not in listed
    assert "auto-balance" in listed


@pytest.mark.asyncio
async def test_the_anthropic_model_list_offers_the_tiers_too(gateway):
    response = await gateway.get("/v1/models", {"anthropic-version": "2023-06-01"})

    ids = [item["id"] for item in response.json()["data"]]
    assert ids[-4:] == ["auto", "auto-intelligence", "auto-balance", "auto-fast"]
