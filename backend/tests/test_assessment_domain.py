from app.config import Settings


VALID_DATABASE_URL = "postgresql+asyncpg://earthquake:earthquake@localhost:5432/earthquake"
VALID_JWT_SECRET = "test-jwt-secret-at-least-16-characters"
VALID_SUPERADMIN_PASSWORD = "test-superadmin-password-at-least-16-characters"


def assessment_settings(**overrides):
    values = {
        "database_url": VALID_DATABASE_URL,
        "jwt_secret": VALID_JWT_SECRET,
        "superadmin_initial_password": VALID_SUPERADMIN_PASSWORD,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_assessment_settings_defaults() -> None:
    configured = assessment_settings()

    assert configured.temporal_address == "temporal:7233"
    assert configured.temporal_namespace == "default"
    assert configured.temporal_task_queue == "assessment"
    assert configured.assessment_dispatcher_enabled is False
    assert configured.assessment_outbox_poll_seconds == 1.0
    assert configured.assessment_outbox_batch_size == 20
    assert configured.assessment_outbox_max_attempts == 10
    assert configured.assessment_outbox_lease_seconds == 60
    assert configured.assessment_workflow_deadline_seconds == 300
    assert configured.intensity_parameters_path.endswith("shanghai-2019.yaml")
    assert configured.intensity_region_profile_path.endswith("shanghai-region.yaml")
    assert configured.assessment_workflow_safety_timeout_seconds == 1800


def test_assessment_settings_validate_bounded_values() -> None:
    invalid_values = (
        {"temporal_address": " "},
        {"temporal_namespace": ""},
        {"temporal_task_queue": " "},
        {"assessment_outbox_poll_seconds": 0},
        {"assessment_outbox_batch_size": 0},
        {"assessment_outbox_batch_size": 1_001},
        {"assessment_outbox_max_attempts": 0},
        {"assessment_outbox_max_attempts": 101},
        {"assessment_outbox_lease_seconds": 0},
        {"assessment_workflow_deadline_seconds": 0},
        {"intensity_parameters_path": " "},
        {"intensity_region_profile_path": ""},
        {"assessment_workflow_safety_timeout_seconds": 0},
    )

    for overrides in invalid_values:
        try:
            assessment_settings(**overrides)
        except ValueError:
            continue
        raise AssertionError(f"expected validation failure for {overrides}")
