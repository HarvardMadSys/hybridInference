"""Anthropic API versions a Messages batch can be created under.

A batch whose target is ``/v1/messages`` names an ``anthropic_version`` in its
``POST /v1/batches`` envelope (inline and file alike). The batch keeps it
(``protocol_profile``), and every request in it is checked against that
version's rules when it is admitted, or when its input file is validated. Two
versions are known:

- ``2023-06-01``, what every SDK before the 2026 major versions sends;
- ``2026-10-21``, what the current SDKs send. Its changes to the Messages API
  are all request-side (Anthropic's migration guide, "Breaking changes"): a
  request sending ``temperature``, ``top_p`` or ``top_k``, a ``document`` block
  placed directly in an ``assistant`` message, or a ``text`` block with
  non-empty ``citations`` in a ``user`` message is an error. A request without
  those samples with the defaults, as a ``2023-06-01`` request leaving them out
  does, and the response shape is the same.

Admission applies the version's rules, so a request accepted under
``2026-10-21`` means the same under either version. The native Anthropic
adapter is sent the stored version (``req_ctx.ANTHROPIC_VERSION``); other
adapters receive a request that the version's rules already made equivalent.
A Messages batch without a version, a version not listed here and any
``anthropic-beta`` header are refused when a batch is created. Which of the
known versions new batches may use is the ``BATCH_ANTHROPIC_VERSIONS``
setting; a batch already stored keeps running under its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from serving.batch.config import KNOWN_ANTHROPIC_VERSIONS


@dataclass(frozen=True)
class AnthropicProfile:
    """The request rules of one ``anthropic-version``."""

    version: str
    #: Top-level parameters this version refuses.
    removed_params: frozenset[str] = frozenset()
    #: ``document`` blocks directly in an ``assistant`` message are refused.
    reject_assistant_documents: bool = False
    #: ``text`` blocks with non-empty ``citations`` in a ``user`` message are refused.
    reject_user_citations: bool = False


PROFILES: dict[str, AnthropicProfile] = {
    "2023-06-01": AnthropicProfile(version="2023-06-01"),
    "2026-10-21": AnthropicProfile(
        version="2026-10-21",
        removed_params=frozenset({"temperature", "top_p", "top_k"}),
        reject_assistant_documents=True,
        reject_user_citations=True,
    ),
}

assert set(PROFILES) == set(KNOWN_ANTHROPIC_VERSIONS)


def profile_violation(profile: AnthropicProfile, params: dict[str, Any]) -> str | None:
    """Return why *params* is invalid under *profile*, or None."""
    removed = sorted(name for name in profile.removed_params if name in params)
    if removed:
        return (
            f"{', '.join(removed)}: not supported with anthropic-version {profile.version}; "
            "remove the parameter or create the batch with anthropic-version 2023-06-01"
        )
    messages = params.get("messages")
    if not isinstance(messages, list):
        return None
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        role = message.get("role")
        for block in content:
            if not isinstance(block, dict):
                continue
            if (
                profile.reject_assistant_documents
                and role == "assistant"
                and block.get("type") == "document"
            ):
                return (
                    f"messages.{index}: document blocks are not accepted in assistant messages "
                    f"with anthropic-version {profile.version}; send them in a user message"
                )
            if profile.reject_user_citations and role == "user" and _has_citations(block):
                return (
                    f"messages.{index}: citations on user text blocks are not accepted "
                    f"with anthropic-version {profile.version}"
                )
    return None


def _has_citations(block: dict[str, Any]) -> bool:
    if block.get("type") == "text" and block.get("citations"):
        return True
    if block.get("type") in ("search_result", "tool_result"):
        inner = block.get("content")
        if isinstance(inner, list):
            return any(isinstance(item, dict) and _has_citations(item) for item in inner)
    return False
