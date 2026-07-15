"""Environment-backed configuration for the triage relay process."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class TriageSettings(BaseSettings):
    """Settings used only by the standalone triage relay.

    The relay no longer runs Codex itself: analysis executes in the
    ``codex-triage`` GitHub Actions workflow, triggered through
    ``repository_dispatch``. ``codex_model`` and ``hybrid_inference_base_url``
    are passed through in the dispatch payload so the workflow and the relay
    stay configured from one place.
    """

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    relay_token: SecretStr = Field(default=SecretStr(""), alias="CODEX_TRIAGE_RELAY_TOKEN")
    slack_bot_token: SecretStr = Field(default=SecretStr(""), alias="CODEX_TRIAGE_SLACK_BOT_TOKEN")
    slack_channel_id: str = Field(default="", alias="CODEX_TRIAGE_SLACK_CHANNEL_ID")
    state_dir: Path = Field(default=Path(".codex-triage"), alias="CODEX_TRIAGE_STATE_DIR")
    github_token: SecretStr = Field(default=SecretStr(""), alias="CODEX_TRIAGE_GITHUB_TOKEN")
    # "owner/repo" that hosts the codex-triage workflow.
    github_repository: str = Field(default="", alias="CODEX_TRIAGE_GITHUB_REPOSITORY")
    github_api_base_url: str = Field(
        default="https://api.github.com",
        alias="CODEX_TRIAGE_GITHUB_API_BASE_URL",
    )
    dispatch_event_type: str = Field(
        default="codex-triage",
        alias="CODEX_TRIAGE_DISPATCH_EVENT_TYPE",
    )
    codex_model: str = Field(default="deepseek-v4-flash", alias="CODEX_TRIAGE_CODEX_MODEL")
    # Must be reachable from GitHub-hosted runners, so the public gateway URL.
    hybrid_inference_base_url: str = Field(
        default="https://freeinference.org/v1",
        alias="CODEX_TRIAGE_HYBRID_BASE_URL",
    )
    worker_poll_seconds: float = Field(
        default=1.0,
        ge=0.05,
        le=60.0,
        alias="CODEX_TRIAGE_POLL_SECONDS",
    )
    max_attempts: int = Field(default=2, ge=1, le=5, alias="CODEX_TRIAGE_MAX_ATTEMPTS")
    max_pending_jobs: int = Field(
        default=100,
        ge=1,
        le=10_000,
        alias="CODEX_TRIAGE_MAX_PENDING_JOBS",
    )

    @property
    def configured(self) -> bool:
        """Return whether the relay has the credentials needed to accept work."""
        return bool(
            self.relay_token.get_secret_value().strip()
            and self.slack_bot_token.get_secret_value().strip()
            and self.slack_channel_id.strip()
            and self.github_token.get_secret_value().strip()
            and self.github_repository.strip()
            and self.codex_model.strip()
            and self.hybrid_inference_base_url.strip()
        )
