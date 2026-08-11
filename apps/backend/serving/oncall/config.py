"""Environment-backed configuration for the oncall relay process."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class OnCallSettings(BaseSettings):
    """Settings used only by the standalone oncall relay.

    The relay never runs Codex itself. Which platform runs the analysis is
    ``dispatch_backend``:

    - ``github`` — the original hand-off: ``repository_dispatch`` triggers the
      ``codex-oncall`` GitHub Actions workflow, which runs Codex on a
      GitHub-hosted runner and posts the result to Slack itself.
      ``codex_model`` and ``model_base_url`` ride the dispatch payload so the
      workflow and the relay stay configured from one place.
    - ``cloud-agent`` — the analysis runs as a job on the FreeInference cloud
      agent platform (its own runner host, a per-attempt inference grant, no
      Actions minutes). The relay creates the job, polls it, and posts the
      result to Slack itself; the ``agent_*`` settings below configure that
      loop. ``model_base_url`` is not used — the platform injects its own
      gateway address into the sandbox.
    """

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    relay_token: SecretStr = Field(default=SecretStr(""), alias="CODEX_ONCALL_RELAY_TOKEN")
    slack_bot_token: SecretStr = Field(default=SecretStr(""), alias="CODEX_ONCALL_SLACK_BOT_TOKEN")
    slack_channel_id: str = Field(default="", alias="CODEX_ONCALL_SLACK_CHANNEL_ID")
    state_dir: Path = Field(default=Path(".codex-oncall"), alias="CODEX_ONCALL_STATE_DIR")
    github_token: SecretStr = Field(default=SecretStr(""), alias="CODEX_ONCALL_GITHUB_TOKEN")
    # "owner/repo" that hosts the codex-oncall workflow.
    github_repository: str = Field(default="", alias="CODEX_ONCALL_GITHUB_REPOSITORY")
    github_api_base_url: str = Field(
        default="https://api.github.com",
        alias="CODEX_ONCALL_GITHUB_API_BASE_URL",
    )
    dispatch_event_type: str = Field(
        default="codex-oncall",
        alias="CODEX_ONCALL_DISPATCH_EVENT_TYPE",
    )
    # glm-5.2 until the H200 DeepSeek-V4 parser fix (PR #939) is deployed and
    # verified; then flip to deepseek-v4-flash (cheaper local route).
    codex_model: str = Field(default="glm-5.2", alias="CODEX_ONCALL_CODEX_MODEL")
    # Codex speaks only the OpenAI Responses API (chat wire support was
    # removed upstream, openai/codex#7782), so this must be an endpoint that
    # serves /v1/responses — in practice this gateway, which translates to
    # Chat Completions southbound. It has to be reachable from GitHub-hosted
    # runners, so a public address rather than a Compose hostname.
    #
    # Empty by default. The previous default was this deployment's public URL,
    # so an operator who never configured on-call would have been dispatching
    # their analysis at someone else's gateway. Required is not an option here
    # — these settings are constructed whether or not on-call is enabled — so
    # it is checked where it is used instead.
    model_base_url: str = Field(default="", alias="CODEX_ONCALL_MODEL_BASE_URL")
    worker_poll_seconds: float = Field(
        default=1.0,
        ge=0.05,
        le=60.0,
        alias="CODEX_ONCALL_POLL_SECONDS",
    )
    max_attempts: int = Field(default=2, ge=1, le=5, alias="CODEX_ONCALL_MAX_ATTEMPTS")
    max_pending_jobs: int = Field(
        default=100,
        ge=1,
        le=10_000,
        alias="CODEX_ONCALL_MAX_PENDING_JOBS",
    )

    # ── cloud-agent backend ────────────────────────────────────────────
    # Which platform runs the analysis. Defaults to the GitHub Actions
    # hand-off so an existing deployment upgrades without a behaviour change;
    # flipping to "cloud-agent" is an explicit .env.oncall edit made together
    # with the agent_* values below.
    dispatch_backend: Literal["github", "cloud-agent"] = Field(
        default="github",
        alias="CODEX_ONCALL_DISPATCH_BACKEND",
    )
    # Control plane origin, e.g. https://agent.example.org. Must serve
    # /v1/agent/service/oncall/* and be reachable from the relay container.
    agent_base_url: str = Field(default="", alias="CODEX_ONCALL_AGENT_BASE_URL")
    # The AGENT_ONCALL_DISPATCH_TOKEN configured on the control plane.
    agent_dispatch_token: SecretStr = Field(
        default=SecretStr(""),
        alias="CODEX_ONCALL_AGENT_DISPATCH_TOKEN",
    )
    # Repository the analysis job checks out; defaults to github_repository so
    # the two backends stay pointed at the same code without double entry.
    agent_repo: str = Field(default="", alias="CODEX_ONCALL_AGENT_REPO")
    # Branch the analysis pins at creation — the platform resolves it to the
    # commit it names right then, which the GHA backend never could.
    agent_base_ref: str = Field(default="dev", alias="CODEX_ONCALL_AGENT_BASE_REF")
    agent_runtime: str = Field(default="codex", alias="CODEX_ONCALL_AGENT_RUNTIME")
    # How often the relay polls a running analysis job, and how long it waits
    # before cancelling it. The GHA workflow capped analysis at 15 minutes;
    # 25 minutes here allows for queueing behind another job on the runner.
    agent_poll_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=300.0,
        alias="CODEX_ONCALL_AGENT_POLL_SECONDS",
    )
    agent_timeout_seconds: float = Field(
        default=1_500.0,
        ge=60.0,
        le=14_400.0,
        alias="CODEX_ONCALL_AGENT_TIMEOUT_SECONDS",
    )
    # Where a human reads the job — "{job_id}" is substituted. Optional: unset
    # renders the bare job id in Slack instead of a link.
    agent_console_url: str = Field(default="", alias="CODEX_ONCALL_AGENT_CONSOLE_URL")

    @property
    def oncall_repo(self) -> str:
        """The repository analysis jobs target, whichever backend runs them."""
        return (self.agent_repo or self.github_repository).strip()

    @property
    def configured(self) -> bool:
        """Return whether the relay has the credentials needed to accept work."""
        common = bool(
            self.relay_token.get_secret_value().strip()
            and self.slack_bot_token.get_secret_value().strip()
            and self.slack_channel_id.strip()
            and self.codex_model.strip()
        )
        if not common:
            return False
        if self.dispatch_backend == "cloud-agent":
            return bool(
                self.agent_base_url.strip()
                and self.agent_dispatch_token.get_secret_value().strip()
                and self.oncall_repo
                and self.agent_runtime.strip()
            )
        return bool(
            self.github_token.get_secret_value().strip()
            and self.github_repository.strip()
            and self.model_base_url.strip()
        )
