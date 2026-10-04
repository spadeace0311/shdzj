import pytest
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.config import settings
from app.data_assets.models import DataAssetSnapshot
from app.db import engine
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from tests.data_asset_helpers import publish_new_population_version


@pytest.fixture(autouse=True)
async def _clean_snapshot_data(session_factory):
    await engine.dispose()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(DataAssetSnapshot))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
    yield
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(DataAssetSnapshot))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
    await engine.dispose()


async def test_assessment_run_locks_available_assets_and_records_missing_required(
    session_factory,
    seeded_outbox,
    published_population_asset,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=seeded_outbox.event_id,
                revision_id=seeded_outbox.revision_id,
                outbox_id=seeded_outbox.outbox_id,
            )
            snapshots = (
                await session.scalars(
                    select(DataAssetSnapshot).where(DataAssetSnapshot.run_id == run.id)
                )
            ).all()

    assert len(snapshots) == 2
    assert {snapshot.asset_key for snapshot in snapshots} == {
        "shanghai.admin.town",
        "shanghai.population.town",
    }
    assert run.snapshot["region_id"] == settings.data_asset_region_id
    assert run.data_asset_snapshot_fingerprint
    assert "shanghai.building.town" in run.data_asset_snapshot_result["missing_required"]


async def test_data_update_after_run_does_not_change_locked_snapshot(
    session_factory,
    seeded_outbox,
    published_population_asset,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=seeded_outbox.event_id,
                revision_id=seeded_outbox.revision_id,
                outbox_id=seeded_outbox.outbox_id,
            )
            original = run.data_asset_snapshot_fingerprint

    await publish_new_population_version(session_factory, "2023.1")

    async with session_factory() as session:
        async with session.begin():
            stored = await session.get(type(run), run.id)
    assert stored.data_asset_snapshot_fingerprint == original
