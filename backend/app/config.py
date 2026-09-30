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
    temporal_address: str = "temporal:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "assessment"
    assessment_dispatcher_enabled: bool = False
    assessment_outbox_poll_seconds: float = 1.0
    assessment_outbox_batch_size: int = 20
    assessment_outbox_max_attempts: int = 10
    assessment_outbox_lease_seconds: int = 60
    intensity_parameters_path: str = "/config/intensity/shanghai-2019.yaml"
    intensity_region_profile_path: str = "/config/intensity/shanghai-region.yaml"
    loss_region_profile_path: str = "/config/loss/shanghai-region.yaml"
    assessment_workflow_safety_timeout_seconds: int = 1800
    data_asset_region_id: str = "shanghai"
    data_asset_required_registry_path: str = "/config/data_assets/shanghai-required-assets.yaml"
    data_asset_coverage_policy_path: str = "/config/data_assets/shanghai-coverage-policy.yaml"
    data_asset_storage_root: str = "/var/lib/data-assets"
    data_asset_max_upload_bytes: int = 1_073_741_824
    data_asset_mdb_driver: str = "Microsoft Access Driver (*.mdb, *.accdb)"
    data_asset_worker_poll_seconds: float = 1.0
    artifact_storage_root: str = "/var/lib/artifacts"
    artifact_template_root: str = "/config/artifact_templates"
    artifact_catalog_path: str = "/config/artifacts/catalog.yaml"
    artifact_max_override_bytes: int = 1_073_741_824
    artifact_render_concurrency: int = 4
    artifact_optional_dependency_reserve_seconds: int = 30
    artifact_browser_pool_size: int = 2

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
        if not self.temporal_address.strip():
            raise ValueError("TEMPORAL_ADDRESS must not be empty")
        if not self.temporal_namespace.strip():
            raise ValueError("TEMPORAL_NAMESPACE must not be empty")
        if not self.temporal_task_queue.strip():
            raise ValueError("TEMPORAL_TASK_QUEUE must not be empty")
        if self.assessment_outbox_poll_seconds <= 0:
            raise ValueError("ASSESSMENT_OUTBOX_POLL_SECONDS must be positive")
        if not 1 <= self.assessment_outbox_batch_size <= 1_000:
            raise ValueError("ASSESSMENT_OUTBOX_BATCH_SIZE must be between 1 and 1000")
        if not 1 <= self.assessment_outbox_max_attempts <= 100:
            raise ValueError("ASSESSMENT_OUTBOX_MAX_ATTEMPTS must be between 1 and 100")
        if self.assessment_outbox_lease_seconds <= 0:
            raise ValueError("ASSESSMENT_OUTBOX_LEASE_SECONDS must be positive")
        if not self.intensity_parameters_path.strip():
            raise ValueError("INTENSITY_PARAMETERS_PATH must not be empty")
        if not self.intensity_region_profile_path.strip():
            raise ValueError("INTENSITY_REGION_PROFILE_PATH must not be empty")
        if not self.loss_region_profile_path.strip():
            raise ValueError("LOSS_REGION_PROFILE_PATH must not be empty")
        if self.assessment_workflow_safety_timeout_seconds <= 0:
            raise ValueError("ASSESSMENT_WORKFLOW_SAFETY_TIMEOUT_SECONDS must be positive")
        if not self.data_asset_storage_root.strip():
            raise ValueError("DATA_ASSET_STORAGE_ROOT must not be empty")
        if not self.data_asset_region_id.strip():
            raise ValueError("DATA_ASSET_REGION_ID must not be empty")
        if not self.data_asset_required_registry_path.strip():
            raise ValueError("DATA_ASSET_REQUIRED_REGISTRY_PATH must not be empty")
        if not self.data_asset_coverage_policy_path.strip():
            raise ValueError("DATA_ASSET_COVERAGE_POLICY_PATH must not be empty")
        if self.data_asset_max_upload_bytes < 1:
            raise ValueError("DATA_ASSET_MAX_UPLOAD_BYTES must be positive")
        if not self.data_asset_mdb_driver.strip():
            raise ValueError("DATA_ASSET_MDB_DRIVER must not be empty")
        if self.data_asset_worker_poll_seconds <= 0:
            raise ValueError("DATA_ASSET_WORKER_POLL_SECONDS must be positive")
        return self

    @model_validator(mode="after")
    def validate_artifact_configuration(self) -> "Settings":
        artifact_paths = (
            self.artifact_storage_root,
            self.artifact_template_root,
            self.artifact_catalog_path,
        )
        for path in artifact_paths:
            if not path.strip():
                raise ValueError("artifact configuration paths must not be empty")
        if self.artifact_max_override_bytes < 1:
            raise ValueError("ARTIFACT_MAX_OVERRIDE_BYTES must be positive")
        if not 1 <= self.artifact_render_concurrency <= 6:
            raise ValueError("ARTIFACT_RENDER_CONCURRENCY must be between 1 and 6")
        if not 1 <= self.artifact_browser_pool_size <= 8:
            raise ValueError("ARTIFACT_BROWSER_POOL_SIZE must be between 1 and 8")
        if not 0 < self.artifact_optional_dependency_reserve_seconds < 300:
            raise ValueError(
                "ARTIFACT_OPTIONAL_DEPENDENCY_RESERVE_SECONDS must be positive "
                "and less than 300"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
