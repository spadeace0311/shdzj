from pydantic import SecretStr

from app.config import Settings


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "jwt_secret": SecretStr("jwt-secret-at-least-16-characters"),
        "superadmin_initial_password": SecretStr("admin-secret-at-least-16-characters"),
        "cenc_collector_enabled": True,
        "fan_app_id": "",
        "fan_api_key": SecretStr("fan-key"),
        "cenc_app_id": "compat-app-id",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_collector_uses_cenc_app_id_as_compatibility_alias() -> None:
    settings = make_settings()

    assert settings.resolved_fan_app_id == "compat-app-id"


def test_collector_is_disabled_by_default_without_credentials() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost/db",
        jwt_secret=SecretStr("jwt-secret-at-least-16-characters"),
        superadmin_initial_password=SecretStr("admin-secret-at-least-16-characters"),
    )

    assert settings.cenc_collector_enabled is False


def test_enabled_collector_requires_api_key() -> None:
    try:
        make_settings(fan_api_key=SecretStr(""))
    except ValueError as exc:
        assert "FAN_API_KEY" in str(exc)
    else:
        raise AssertionError("collector configuration must reject an empty API key")


def test_collector_can_be_disabled_without_credentials() -> None:
    settings = make_settings(
        cenc_collector_enabled=False,
        fan_api_key=SecretStr(""),
        cenc_app_id="",
    )

    assert settings.cenc_collector_enabled is False
