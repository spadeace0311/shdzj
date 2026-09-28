import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings

VALID_DATABASE_URL = "postgresql+asyncpg://earthquake:earthquake@localhost:5432/earthquake"
VALID_JWT_SECRET = "test-jwt-secret-at-least-16-characters"
VALID_SUPERADMIN_PASSWORD = "test-superadmin-password-at-least-16-characters"
INSECURE_VALUES = (
    "development-secret",
    "development-password",
    "replace-this-before-deployment",
)


def test_runtime_secrets_are_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_SECRET")
    monkeypatch.delenv("SUPERADMIN_INITIAL_PASSWORD")

    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=VALID_DATABASE_URL)


@pytest.mark.parametrize("value", INSECURE_VALUES)
@pytest.mark.parametrize("field_name", ("jwt_secret", "superadmin_initial_password"))
def test_runtime_secrets_reject_known_placeholders(
    field_name: str,
    value: str,
) -> None:
    values = {
        "database_url": VALID_DATABASE_URL,
        "jwt_secret": VALID_JWT_SECRET,
        "superadmin_initial_password": VALID_SUPERADMIN_PASSWORD,
    }
    values[field_name] = value

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


@pytest.mark.parametrize("field_name", ("jwt_secret", "superadmin_initial_password"))
def test_runtime_secrets_enforce_minimum_length(field_name: str) -> None:
    values = {
        "database_url": VALID_DATABASE_URL,
        "jwt_secret": VALID_JWT_SECRET,
        "superadmin_initial_password": VALID_SUPERADMIN_PASSWORD,
    }
    values[field_name] = "x" * 15

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_runtime_secrets_are_secret_values() -> None:
    configured = Settings(
        _env_file=None,
        database_url=VALID_DATABASE_URL,
        jwt_secret=VALID_JWT_SECRET,
        superadmin_initial_password=VALID_SUPERADMIN_PASSWORD,
    )

    assert isinstance(configured.jwt_secret, SecretStr)
    assert isinstance(configured.superadmin_initial_password, SecretStr)
    assert configured.jwt_secret.get_secret_value() == VALID_JWT_SECRET
    assert configured.superadmin_initial_password.get_secret_value() == VALID_SUPERADMIN_PASSWORD
