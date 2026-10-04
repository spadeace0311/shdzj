import os
from pathlib import Path

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


def test_artifact_template_root_default_matches_runtime_contract() -> None:
    configured = Settings(
        _env_file=None,
        database_url=VALID_DATABASE_URL,
        jwt_secret=VALID_JWT_SECRET,
        superadmin_initial_password=VALID_SUPERADMIN_PASSWORD,
    )

    assert configured.artifact_template_root == "/config/artifacts/templates"


def test_artifact_browser_pool_defaults_stay_in_sync() -> None:
    project_root = Path(
        os.environ.get(
            "PROJECT_ROOT",
            Path(__file__).resolve().parents[2],
        )
    )
    example = (project_root / ".env.example").read_text(encoding="utf-8")
    compose = (project_root / "infra" / "compose.yaml").read_text(
        encoding="utf-8"
    )
    configured = Settings(
        _env_file=None,
        database_url=VALID_DATABASE_URL,
        jwt_secret=VALID_JWT_SECRET,
        superadmin_initial_password=VALID_SUPERADMIN_PASSWORD,
    )

    assert "ARTIFACT_BROWSER_POOL_SIZE=2" in example
    assert (
        'ARTIFACT_BROWSER_POOL_SIZE: "${ARTIFACT_BROWSER_POOL_SIZE:-2}"'
        in compose
    )
    assert configured.artifact_browser_pool_size == 2
