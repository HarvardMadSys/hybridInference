"""Authentication secret validation shares the runtime credential source."""

import pytest

from serving.config.settings import Settings, get_settings
from serving.utils.jwt import get_jwt_secret


@pytest.mark.parametrize("missing_key", ["jwt_secret_key", "api_key_secret"])
def test_error_names_only_missing_secrets(missing_key):
    values = {
        "jwt_secret_key": "jwt-secret-must-not-appear-in-errors",
        "api_key_secret": "api-secret-must-not-appear-in-errors",
        missing_key: "",
    }
    settings = Settings(_env_file=None, **values)

    with pytest.raises(ValueError) as error:
        settings.validate_auth_secrets(database_enabled=True)

    assert missing_key.upper() in str(error.value)
    assert "must-not-appear-in-errors" not in str(error.value)


def test_validation_never_trims_or_replaces_configured_secrets():
    settings = Settings(
        _env_file=None,
        jwt_secret_key="  existing-jwt-secret  ",
        api_key_secret="  existing-api-secret  ",
    )

    settings.validate_auth_secrets(database_enabled=True)

    assert settings.jwt_secret_key == "  existing-jwt-secret  "
    assert settings.api_key_secret == "  existing-api-secret  "


def test_runtime_enable_override_cannot_use_auth_disabled_exemption():
    settings = Settings(
        _env_file=None, user_auth_enabled=False, jwt_secret_key="", api_key_secret=""
    )

    with pytest.raises(ValueError, match="JWT_SECRET_KEY, API_KEY_SECRET"):
        settings.validate_auth_secrets(database_enabled=False, user_auth_enabled=True)


def test_jwt_reader_uses_the_settings_validated_at_startup(monkeypatch, tmp_path):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "JWT_SECRET_KEY=test-dotenv-jwt-secret\nAPI_KEY_SECRET=test-dotenv-api-secret\n"
    )
    get_settings.cache_clear()
    settings = get_settings()

    settings.validate_auth_secrets(database_enabled=True)

    assert get_jwt_secret() == settings.jwt_secret_key == "test-dotenv-jwt-secret"


def test_jwt_reader_rejects_whitespace_only_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", " \t\n")

    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        get_jwt_secret()
