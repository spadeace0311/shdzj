from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str = "development-secret"
    jwt_expire_minutes: int = 480
    superadmin_username: str = "superadmin"
    superadmin_initial_password: str = "development-password"
    cenc_app_id: str = ""
    cenc_api_base_url: str = ""
    response_rules_path: str = "/config/response_rules/shanghai-2026.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
