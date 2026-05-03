"""Data models for the standalone harness."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _mask_secret(value: str) -> str:
    """Redacts a secret while keeping a short prefix for debugging."""
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


@dataclass(frozen=True)
class Capabilities:
    """Capability flags declared by each target."""

    chat: bool = True
    streaming: bool = True
    tools: bool = False
    structured_output: bool = False
    embeddings: bool = False
    anthropic_messages: bool = False
    admin_required: bool = False

    def supports(self, capability: str) -> bool:
        """Returns whether the target declares a given capability."""
        return bool(getattr(self, capability, False))


@dataclass(frozen=True)
class TargetConfig:
    """Configuration for a single runnable target."""

    name: str
    model: str
    base_url: str
    api_key: str
    suite_type: str
    timeout_seconds: float = 240.0
    sampling_count: int = 5
    capabilities: Capabilities = field(default_factory=Capabilities)
    tags: tuple[str, ...] = ()
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioConfig:
    """Configuration for a single scenario."""

    scenario_id: str
    scenario_type: str
    required_capabilities: tuple[str, ...] = ()
    repetitions: int | None = None
    max_tokens: int | None = None
    tools_fixture: str | None = None
    forced_tool_name: str | None = None
    user_prompt: str | None = None


@dataclass(frozen=True)
class SuiteConfig:
    """Configuration for a suite of scenarios."""

    suite_name: str
    scenarios: tuple[ScenarioConfig, ...]


@dataclass
class AttemptResult:
    """Result for a single scenario attempt."""

    target_name: str
    target_model: str
    suite_name: str
    scenario_id: str
    scenario_type: str
    repetition: int
    status: str
    failure_type: str | None
    latency_ms: int | None
    http_status: int | None
    detail: str
    observed: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serializes the attempt result."""
        return asdict(self)


@dataclass
class ScenarioSummary:
    """Aggregated summary for a target/scenario pair."""

    target_name: str
    target_model: str
    suite_name: str
    scenario_id: str
    scenario_type: str
    attempts: list[AttemptResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serializes the scenario summary."""
        attempts = [attempt.to_dict() for attempt in self.attempts]
        total = len(attempts)
        passed = sum(1 for attempt in attempts if attempt["status"] == "pass")
        failed = sum(1 for attempt in attempts if attempt["status"] == "fail")
        skipped = sum(1 for attempt in attempts if attempt["status"] == "skip")
        scored = passed + failed
        return {
            "target_name": self.target_name,
            "target_model": self.target_model,
            "suite_name": self.suite_name,
            "scenario_id": self.scenario_id,
            "scenario_type": self.scenario_type,
            "total_attempts": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "pass_rate": (passed / scored) if scored else 0.0,
            "attempts": attempts,
        }


@dataclass
class RunRecord:
    """Artifacts for one harness execution."""

    run_id: str
    timestamp: str
    suite_name: str
    targets: list[TargetConfig]
    scenario_summaries: list[ScenarioSummary]

    def to_dict(self) -> dict[str, Any]:
        """Serializes the run record."""
        target_dicts: list[dict[str, Any]] = []
        for target in self.targets:
            target_data = asdict(target)
            target_data["api_key"] = _mask_secret(target.api_key)
            target_dicts.append(target_data)
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "suite_name": self.suite_name,
            "targets": target_dicts,
            "scenario_summaries": [summary.to_dict() for summary in self.scenario_summaries],
        }
