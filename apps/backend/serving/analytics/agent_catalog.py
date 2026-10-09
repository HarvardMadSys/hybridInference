"""Identify the client software behind a request, for aggregate usage stats.

Two signals are recorded at log time:

* ``metadata.agent``: the name an agent declares in its system prompt
  (``"You are Claude Code, ..."``), extracted by
  :func:`serving.storage.utils.agent_name_from_prompt`.
* ``metadata.user_agent``: the HTTP ``User-Agent`` header.

:func:`classify` maps that pair onto a catalog of known agent products, and
:func:`client_name` reduces a User-Agent to its product token
(``"codex-tui/0.160.0 (Pop!_OS ...)"`` -> ``"codex-tui"``) so distinct clients
can be counted across versions and platforms. Generic HTTP libraries, SDKs and
browsers carry no client identity of their own; :func:`is_generic_client`
recognises them so they are not counted as clients.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CODING = "coding"
GENERAL = "general"
CHAT = "chat"
CUSTOM = "custom"
DIRECT = "direct"

# Order is the display order of the kinds.
KINDS = (CODING, GENERAL, CUSTOM, CHAT, DIRECT)
AGENT_KINDS = frozenset({CODING, GENERAL})


@dataclass(frozen=True)
class Product:
    """A known client product and how to recognise it."""

    name: str
    kind: str
    declared: frozenset[str]  # lower-cased names declared in a system prompt
    user_agent: re.Pattern[str] | None


def _p(name: str, kind: str, declared: tuple[str, ...] = (), ua: str | None = None) -> Product:
    return Product(name, kind, frozenset(declared), re.compile(ua, re.I) if ua else None)


CATALOG: tuple[Product, ...] = (
    _p("Claude Code", CODING, ("claude",), r"^(claude-cli|claude-code)/"),
    _p("Codex", CODING, ("codex",), r"^(codex[-_ ]|codex_|Codex[ -])"),
    _p("OpenCode", CODING, ("opencode",), r"^opencode/"),
    _p("Kilo Code", CODING, ("kilo",), r"^(Kilo-Code|kilo/|opencode-kilo-provider)"),
    _p("pi", CODING, ("pi",), r"^(pi \(|pi/|pi-coding-agent)"),
    _p("oh-my-pi", CODING, ("omp",), r"^(omp/|oh-my-pi)"),
    _p("Factory Droid", CODING, ("droid",), r"^factory-cli/"),
    _p("Grok CLI", CODING, ("grok",), r"^grok-(shell|pager)/"),
    _p("ZCode", CODING, ("zcode",), r"^ZCode"),
    _p("Cline", CODING, ("cline",), r"^Cline/"),
    _p("Roo Code", CODING, ("roo",), r"^RooCode"),
    _p("Zoo Code", CODING, ("zoo",), r"^ZooCode"),
    _p("Crush", CODING, ("crush",), r"^Charm-Crush"),
    _p("Qwen Code", CODING, ("qwen",), r"^QwenCode"),
    _p("GitHub Copilot", CODING, (), r"^GitHubCopilotChat"),
    _p("Zed", CODING, (), r"^Zed/"),
    _p("Cursor", CODING, (), r"^Cursor"),
    _p("jcode", CODING, (), r"^jcode/"),
    _p("CodeBuddy", CODING, ("codebuddy",), r"^(CodeBuddy|WorkBuddy)"),
    _p("MiMo Code", CODING, ("mimocode",), r"^mimocode/"),
    _p("OpenHands", CODING, ("openhands",), r"^OpenHands"),
    _p("OpenClaude", CODING, ("openclaude",), r"^openclaude"),
    _p("gptme", CODING, ("gptme",), r"^gptme"),
    _p("SWE-agent", CODING, (), r"^swe-agent/"),
    _p("Aider", CODING, ("aider",), r"^aider/"),
    _p("Continue", CODING, (), r"^continue/"),
    _p("Gemini CLI", CODING, (), r"^GeminiCLI/"),
    _p("Hermes Agent", GENERAL, ("hermes",), r"^(HermesAgent|hermes-agent|Hermes-Agent)"),
    _p("OpenClaw", GENERAL, ("openclaw",), r"^OpenClaw"),
    _p("DeepSeek Harness", GENERAL, (), r"^deepseek-harness/"),
    _p("Minis", GENERAL, ("minis",), r"^Minis/"),
    _p("AstrBot", GENERAL, (), r"^astrbot"),
    _p("Docs assistant", CHAT, (), r"^doc_assistant$"),
    _p("Cherry Studio", CHAT, (), r"CherryStudio"),
    _p("Chatbox", CHAT, (), r"chatboxapp"),
    _p("Open WebUI", CHAT, (), r"open-webui"),
    _p("AnythingLLM", CHAT, (), r"^AnythingLLM"),
    _p("RikkaHub", CHAT, (), r"^RikkaHub"),
    _p("Kelivo", CHAT, (), r"^Kelivo"),
    _p("Tavo", CHAT, (), r"^Tavo"),
    _p("OpenCat", CHAT, (), r"^OpenCat"),
)

_BY_DECLARED = {name: product for product in CATALOG for name in product.declared}

# HTTP libraries, SDKs, runtimes and browsers: they say how a request was sent,
# not which client sent it.
_GENERIC = re.compile(
    r"^(OpenAI|AsyncOpenAI|openai|python-|Python|node|Bun|undici|curl|Go-http-client|okhttp|"
    r"litellm|langchain|pydantic-ai|ai-sdk|ai/|axios|aiohttp|httpx|Java-http-client|Dart|ktor|"
    r"Deno|ureq|fasthttp|hertz|Dalvik|N/JS|PostmanRuntime|Mozilla|Chrome|tauri|OpenAIClient|"
    r"req/|Ruby|nushell|wget|Anthropic)",
    re.I,
)
_PRODUCT_TOKEN = re.compile(r"^([A-Za-z][\w .+-]*?)(?:/|\s*\(|$)")
_TRAILING_VERSION = re.compile(r"[-_ ]?v?\d[\w.]*$")


def is_generic_client(user_agent: str | None) -> bool:
    """Return True for an empty User-Agent or one naming a generic library."""
    ua = (user_agent or "").strip()
    return not ua or bool(_GENERIC.search(ua))


def client_name(user_agent: str | None) -> str:
    """Return the User-Agent's product token, lower-cased, without version or platform.

    Returns ``""`` when the header carries no usable name.
    """
    ua = re.sub(r"^[^A-Za-z]+", "", (user_agent or "").strip())
    if not ua:
        return ""
    match = _PRODUCT_TOKEN.match(ua)
    name = (match.group(1) if match else ua.split()[0]).strip().lower()
    return _TRAILING_VERSION.sub("", name)


def classify(agent: str | None, user_agent: str | None) -> tuple[str | None, str]:
    """Return ``(product name or None, kind)`` for a request.

    A catalog name declared in the system prompt wins, then a catalog
    User-Agent. Anything else that declares a name or sends a non-generic
    User-Agent is a custom client; the rest is direct API use.
    """
    declared = (agent or "").strip()
    product = _BY_DECLARED.get(declared.lower())
    if product is not None:
        return product.name, product.kind
    ua = (user_agent or "").strip()
    for product in CATALOG:
        if product.user_agent is not None and product.user_agent.search(ua):
            return product.name, product.kind
    if declared or not is_generic_client(ua):
        return None, CUSTOM
    return None, DIRECT
