"""Environment-backed configuration for the triage relay process."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class TriageSettings(BaseSettings):
    """Settings used only by the standalone triage service."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    relay_token: SecretStr = Field(default=SecretStr(""), alias="CODEX_TRIAGE_RELAY_TOKEN")
    slack_bot_token: SecretStr = Field(default=SecretStr(""), alias="CODEX_TRIAGE_SLACK_BOT_TOKEN")
    slack_channel_id: str = Field(default="", alias="CODEX_TRIAGE_SLACK_CHANNEL_ID")
    repository_path: Path = Field(default=Path.cwd(), alias="CODEX_TRIAGE_REPOSITORY_PATH")
    state_dir: Path = Field(default=Path(".codex-triage"), alias="CODEX_TRIAGE_STATE_DIR")
    codex_home: Path | None = Field(default=None, alias="CODEX_TRIAGE_CODEX_HOME")
    codex_binary: str = Field(default="codex", alias="CODEX_TRIAGE_CODEX_BINARY")
    codex_model: str = Field(default="deepseek-v4-pro", alias="CODEX_TRIAGE_CODEX_MODEL")
    hybrid_inference_base_url: str = Field(
        default="https://freeinference.org/v1",
        alias="CODEX_TRIAGE_HYBRID_BASE_URL",
    )
    codex_api_key: SecretStr = Field(default=SecretStr(""), alias="CODEX_API_KEY")
    codex_timeout_seconds: int = Field(
        default=600,
        ge=30,
        le=3_600,
        alias="CODEX_TRIAGE_TIMEOUT_SECONDS",
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
            and self.codex_model.strip()
            and self.hybrid_inference_base_url.strip()
            and self.codex_api_key.get_secret_value().strip()
        )
