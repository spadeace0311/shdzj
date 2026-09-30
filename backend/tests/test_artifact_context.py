import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.artifacts.context import ProductionContextService
from app.artifacts.models import ArtifactTemplate, ArtifactTemplateVersion
from app.artifacts.repository import ArtifactProductionRepository
from app.assessment.models import AssessmentRun
from app.events.models import EarthquakeEvent


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
def production_context_service() -> ProductionContextService:
    return ProductionContextService(
        repository=ArtifactProductionRepository(),
    )


@pytest.fixture(autouse=True)
async def _seed_template_versions_and_context_methods(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            for definition in seeded_artifact_assessment.catalog.definitions:
                template_key = definition.template_key
                template = await session.scalar(
                    select(ArtifactTemplate).where(
                        ArtifactTemplate.template_key == template_key
                    )
                )
                if template is None:
                    template = ArtifactTemplate(
                        template_key=template_key,
                        kind=definition.kind.value,
                        display_name=definition.display_name,
                    )
                    session.add(template)
                    await session.flush()
                existing = await session.scalar(
                    select(ArtifactTemplateVersion).where(
                        ArtifactTemplateVersion.template_id == template.id,
                        ArtifactTemplateVersion.version == "v1",
                    )
                )
                if existing is None:
                    session.add(
                        ArtifactTemplateVersion(
                            template_id=template.id,
                            version="v1",
                            status="published",
                            manifest={"version": "v1"},
                            checksum=hashlib.sha256(
                                f"{template_key}:v1".encode()
                            ).hexdigest(),
                            storage_path=f"templates/{template_key}/v1",
                            created_by="context-fixture",
                            published_at=datetime.now(UTC),
                        )
                    )

    original_create_full_run = seeded_artifact_assessment.create_full_run

    async def create_full_run(
        *,
        revision_no: int = 1,
        production_mode: str = "live",
        missing_optional_assets=None,
        t1_at=None,
    ):
        _ = missing_optional_assets
        if t1_at is None:
            async with session_factory() as session:
                async with session.begin():
                    event = await session.get(
                        EarthquakeEvent,
                        seeded_artifact_assessment.event_id,
                    )
                    assessment = await session.get(
                        AssessmentRun,
                        seeded_artifact_assessment.assessment_run_id,
                    )
                    event.t1_at = None
                    assessment.t1_at = None
        return await original_create_full_run(
            revision_no=revision_no,
            production_mode=production_mode,
        )

    seeded_artifact_assessment.create_full_run = create_full_run
    seeded_artifact_assessment.publish_new_template_version = lambda *_: None
    yield


async def test_static_context_is_deterministic_and_does_not_drift(
    seeded_artifact_assessment,
    production_context_service,
    session,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    first = await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )
    seeded_artifact_assessment.publish_new_template_version("map.epicenter", "v2")
    second = await production_context_service.build_task_context(
        session,
        (await seeded_artifact_assessment.first_task(run.id)).id,
    )

    assert len(first.catalog_version) == len("2026.09.30-professional-v1")
    assert second.context_fingerprint == first.context_fingerprint
    assert second.template_versions["map.epicenter"]["version"] == "v1"


async def test_missing_but_optional_asset_is_recorded_not_invented(
    seeded_artifact_assessment,
    production_context_service,
    session,
) -> None:
    run = await seeded_artifact_assessment.create_full_run(
        missing_optional_assets={"shanghai.reservoir"},
    )
    snapshot = await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )

    item = snapshot.item("shanghai.reservoir")
    assert item.resolution_status == "missing"
    assert item.asset_version_id is None
    assert item.checksum is None


async def test_t1_none_is_serialized_as_null(
    seeded_artifact_assessment,
    production_context_service,
    session,
) -> None:
    run = await seeded_artifact_assessment.create_full_run(t1_at=None)
    context = await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )

    assert context.manifest["t1_at"] is None
