"""Auto models on the inference surfaces (``serving/auto_mode.py``).

A request naming ``auto-<tier>`` is rewritten to the model the tier chooses and
then served as a request naming that model. These tests drive the real handlers
and router: the candidates are three models with different windows and
capabilities, a provider is taken down through the admin kill switch, and the
switch that turns auto on is the runtime setting itself.
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
from routing.executor import RouteExecutor
from routing.model_selection import parse_auto_config
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.auto_mode import AUTO_MODE_SETTING_KEY, AutoMode
from serving.config.runtime_settings import RuntimeSettings
from serving.servers.auth import optional_verify_api_key, verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import anthropic_messages, completions, models, responses

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
        self.router = RouteExecutor()
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
        for module in (completions, anthropic_messages, models, responses):
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


@pytest.fixture
def gateway(mock_db_logger, mock_log_store) -> _Gateway:
    return _Gateway(mock_db_logger, mock_log_store)


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
    assert "X-Auto-Profile" not in response.headers
    logged = await gateway.logged()
    assert logged["model_id"] == MID
    assert "auto" not in logged["metadata"]


# -- Chat Completions -------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_is_served_by_the_tiers_first_choice_and_says_so(gateway):
    response = await gateway.post("/v1/chat/completions", _chat("auto-balance"), SESSION)

    assert response.status_code == 200
    assert response.json()["model"] == MID
    assert response.headers["X-Auto-Profile"] == "balance"
    assert response.headers["X-Auto-Model"] == MID
    assert response.headers["X-Auto-Session"] == "new"
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
    assert response.headers["X-Auto-Profile"] == "balance"


@pytest.mark.asyncio
async def test_a_streamed_chat_carries_the_headers_too(gateway):
    response = await gateway.post("/v1/chat/completions", _chat("auto-balance", stream=True))

    assert response.status_code == 200
    assert response.headers["X-Auto-Model"] == MID
    assert f'"model": "{MID}"' in response.text or f'"model":"{MID}"' in response.text


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
    first = await gateway.post("/v1/messages", _anthropic("auto-balance", OPENING), SESSION)
    gateway.down()

    in_loop = await gateway.post("/v1/messages", _anthropic("auto-balance", _tool_loop()), SESSION)
    next_turn = await gateway.post(
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

    assert [r.headers["X-Auto-Session"] for r in (first, in_loop, next_turn)] == [
        "new",
        "keep",
        "turn_switch",
    ]
    assert [r.headers["X-Auto-Model"] for r in (first, in_loop, next_turn)] == [
        FRONTIER,
        FRONTIER,
        MID,
    ]


@pytest.mark.asyncio
async def test_a_tool_loop_moves_at_once_when_its_model_goes_down(gateway):
    await gateway.post("/v1/messages", _anthropic("auto-balance", OPENING), SESSION)
    gateway.down(MID)

    response = await gateway.post("/v1/messages", _anthropic("auto-balance", _tool_loop()), SESSION)

    assert response.status_code == 200
    assert response.headers["X-Auto-Session"] == "forced_switch"
    assert response.headers["X-Auto-Model"] == FRONTIER


@pytest.mark.asyncio
async def test_a_conversation_in_the_same_session_keeps_its_own_model(gateway):
    """A subagent's conversation, under the session of the main one, is ranked as new.

    The main conversation fell back to frontier-a and is in its tool loop. A
    subagent starts after mid-b recovered and gets mid-b; the main loop stays.
    """
    gateway.down(MID)
    await gateway.post("/v1/messages", _anthropic("auto-balance", OPENING), SESSION)
    gateway.down()

    subagent = await gateway.post(
        "/v1/messages",
        _anthropic("auto-balance", [{"role": "user", "content": "search the docs"}]),
        SESSION,
    )
    main = await gateway.post("/v1/messages", _anthropic("auto-balance", _tool_loop()), SESSION)

    assert (subagent.headers["X-Auto-Session"], subagent.headers["X-Auto-Model"]) == ("new", MID)
    assert (main.headers["X-Auto-Session"], main.headers["X-Auto-Model"]) == ("keep", FRONTIER)


@pytest.mark.asyncio
async def test_sessions_are_kept_per_caller(mock_db_logger, mock_log_store):
    """Another caller declaring the same session id does not inherit its model."""
    gateway = _Gateway(mock_db_logger, mock_log_store)
    gateway.down(MID)
    await gateway.post("/v1/messages", _anthropic("auto-balance", OPENING), SESSION)
    gateway.down()
    other = {**USER, "user_id": "user-2"}
    gateway.app.dependency_overrides[verify_api_key] = lambda: other

    response = await gateway.post("/v1/messages", _anthropic("auto-balance", _tool_loop()), SESSION)

    assert response.headers["X-Auto-Session"] == "new"
    assert response.headers["X-Auto-Model"] == MID


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
    kept = await gateway.post(
        "/v1/messages",
        _anthropic("auto-balance", [*OPENING, by_mid, result, by_frontier, result]),
        SESSION,
    )

    assert switched.headers["X-Auto-Session"] == "forced_switch"
    assert kept.headers["X-Auto-Session"] == "keep"
    first, second = gateway.adapters[FRONTIER].calls
    assert [b["type"] for b in first["messages"][1]["content"]] == ["tool_use"]
    assert [b["type"] for b in second["messages"][1]["content"]] == ["tool_use"]
    assert [b["type"] for b in second["messages"][3]["content"]] == ["thinking", "tool_use"]
    logged = await gateway.logged()
    assert logged["prompt"][1]["content"][0] == _thinking("mid")


@pytest.mark.asyncio
async def test_messages_is_served_and_logged_under_the_chosen_model(gateway):
    response = await gateway.post("/v1/messages", _anthropic("auto-intelligence"), SESSION)

    assert response.status_code == 200
    assert response.headers["X-Auto-Model"] == FRONTIER
    assert gateway.served() == [FRONTIER]
    assert gateway.last_call(FRONTIER)["params"]["reasoning_effort"] == "high"
    logged = await gateway.logged()
    assert logged["model_id"] == FRONTIER
    assert logged["request_payload"]["model"] == "auto-intelligence"
    assert "output_config" not in logged["request_payload"]
    assert logged["metadata"]["alias_input"] is None
    assert logged["metadata"]["auto"]["effort_applied"] == "high"


@pytest.mark.asyncio
async def test_messages_with_tools_skips_a_model_that_would_drop_them(gateway):
    tools = [{"name": "ls", "description": "list", "input_schema": {"type": "object"}}]

    response = await gateway.post("/v1/messages", _anthropic("auto-fast", tools=tools))

    assert response.status_code == 200
    assert response.headers["X-Auto-Model"] == MID
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
async def test_the_responses_api_reports_the_model_that_served(gateway):
    response = await gateway.post("/v1/responses", {"model": "auto-balance", "input": "hi"})

    assert response.status_code == 200
    assert response.json()["model"] == MID
    assert response.headers["X-Auto-Model"] == MID


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
