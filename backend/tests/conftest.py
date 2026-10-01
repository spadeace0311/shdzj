import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://earthquake:earthquake@postgres:5432/earthquake",
)
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-16-characters")
os.environ.setdefault(
    "SUPERADMIN_INITIAL_PASSWORD",
    "test-superadmin-password-at-least-16-characters",
)

from app.db import SessionFactory  # noqa: E402

from app.artifacts.retention import ArtifactRetentionService  # noqa: E402

from tests.artifact_helpers import (  # noqa: E402, F401
    artifact_acceptance_environment,
    artifact_repository,
    seeded_artifact_assessment,
    session,
)
from tests.data_asset_helpers import (  # noqa: E402, F401
    candidate_factory,
    data_asset_client,
    geojson_town_file,
    published_population_asset,
    seeded_assessment_run,
    seeded_imported_version,
    seeded_outbox,
)

_original_cenc_collector_enabled = os.environ.get("CENC_COLLECTOR_ENABLED")
os.environ["CENC_COLLECTOR_ENABLED"] = "false"


@pytest.fixture
def session_factory():
    return SessionFactory


@pytest.fixture
def artifact_retention_service():
    return ArtifactRetentionService()


def pytest_unconfigure(config: object) -> None:
    if _original_cenc_collector_enabled is None:
        os.environ.pop("CENC_COLLECTOR_ENABLED", None)
    else:
        os.environ["CENC_COLLECTOR_ENABLED"] = _original_cenc_collector_enabled
