"""Tests for privacy-first database logging defaults."""

from serving.config.settings import Settings
from serving.storage.database import DatabaseLogger


def test_settings_disable_full_content_storage_by_default(monkeypatch):
    monkeypatch.delenv("DB_STORE_FULL_CONTENT", raising=False)
    monkeypatch.delenv("ADMIN_SHOW_REQUEST_CONTENT", raising=False)

    settings = Settings()

    assert settings.db_store_full_content is False
    assert settings.admin_show_request_content is False


def test_database_logger_disables_full_prompt_storage_by_default():
    logger = DatabaseLogger(db_config={})

    assert logger.store_full_prompts is False
