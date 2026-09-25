from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RUNTIME_SECRET_MIN_LENGTH = 16
INSECURE_RUNTIME_SECRETS = {
    "development-secret",
    "development-password",
    "replace-this-before-deployment",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: SecretStr
    jwt_expire_minutes: int = 480
    superadmin_username: str = "superadmin"
    superadmin_initial_password: SecretStr
    cenc_app_id: str = ""
    cenc_api_base_url: str = ""
    response_rules_path: str = "/config/response_rules/shanghai-2026.yaml"

    @field_validator("jwt_secret", "superadmin_initial_password")
    @classmethod
    def validate_runtime_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if len(secret) < RUNTIME_SECRET_MIN_LENGTH:
            raise ValueError(
                f"runtime secrets must be at least {RUNTIME_SECRET_MIN_LENGTH} characters"
            )
        if secret in INSECURE_RUNTIME_SECRETS:
            raise ValueError("runtime secrets must not use a known placeholder value")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
