from functools import lru_cache

from pydantic import SecretStr, field_validator, model_validator
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
    cenc_collector_enabled: bool = False
    fan_app_id: str = ""
    fan_api_key: SecretStr = SecretStr("")
    cenc_app_id: str = ""
    cenc_api_base_url: str = ""
    fan_ws_primary_url: str = "wss://ws.fanstudio.tech/all"
    fan_ws_backup_url: str = "wss://ws.fanstudio.hk/all"
    fan_query_interval_seconds: int = 10
    wolfx_cenc_url: str = "https://api.wolfx.jp/cenc_eqlist.json"
    wolfx_poll_interval_seconds: int = 10
    cenc_bootstrap_lookback_hours: int = 24
    collector_stale_after_seconds: int = 60
    collector_spool_dir: str = "/var/lib/collector-spool"
    collector_max_spool_bytes: int = 1_073_741_824
    response_rules_path: str = "/config/response_rules/shanghai-2026.yaml"

    @property
    def resolved_fan_app_id(self) -> str:
        return self.fan_app_id.strip() or self.cenc_app_id.strip()

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

    @model_validator(mode="after")
    def validate_collector_configuration(self) -> "Settings":
        if self.cenc_collector_enabled and not self.resolved_fan_app_id:
            raise ValueError(
                "FAN_APP_ID or CENC_APP_ID is required when the collector is enabled"
            )
        if self.cenc_collector_enabled and not self.fan_api_key.get_secret_value():
            raise ValueError("FAN_API_KEY is required when the collector is enabled")
        if self.fan_query_interval_seconds < 1 or self.wolfx_poll_interval_seconds < 1:
            raise ValueError("collector poll intervals must be positive")
        if self.cenc_bootstrap_lookback_hours < 0:
            raise ValueError("CENC_BOOTSTRAP_LOOKBACK_HOURS must not be negative")
        if self.collector_stale_after_seconds < 1:
            raise ValueError("COLLECTOR_STALE_AFTER_SECONDS must be positive")
        if not self.collector_spool_dir.strip():
            raise ValueError("COLLECTOR_SPOOL_DIR must not be empty")
        if self.collector_max_spool_bytes < 1:
            raise ValueError("COLLECTOR_MAX_SPOOL_BYTES must be positive")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
