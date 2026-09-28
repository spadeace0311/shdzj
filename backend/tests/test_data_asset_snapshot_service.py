import pytest
from sqlalchemy import select

from app.config import settings
from app.data_assets.models import DataAssetSnapshot
from app.data_assets.snapshot_service import DataAssetSnapshotService
from tests.data_asset_helpers import publish_new_population_version


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def test_snapshot_records_published_versions_and_missing_required_assets(
    session_factory,
    seeded_assessment_run,
    published_population_asset,
) -> None:
    run_id = seeded_assessment_run
    async with session_factory() as session:
        async with session.begin():
            result = await DataAssetSnapshotService().capture_required_assets(
                session,
                run_id=run_id,
                region_id=settings.data_asset_region_id,
                strict=False,
            )

    assert result.snapshot_count == 2
    assert "shanghai.building.town" in result.missing_required
    assert len(result.fingerprint) == 64
    async with session_factory() as session:
        snapshots = (await session.scalars(select(DataAssetSnapshot))).all()
    assert {snapshot.asset_key for snapshot in snapshots} == {
        "shanghai.admin.town",
        "shanghai.population.town",
    }


async def test_repeated_capture_reuses_locked_versions(
    session_factory,
    seeded_assessment_run,
    published_population_asset,
) -> None:
    run_id = seeded_assessment_run
    async with session_factory() as session:
        async with session.begin():
            first = await DataAssetSnapshotService().capture_required_assets(
                session,
                run_id=run_id,
                region_id=settings.data_asset_region_id,
                strict=False,
            )

    await publish_new_population_version(session_factory, "2022.2")

    async with session_factory() as session:
        async with session.begin():
            second = await DataAssetSnapshotService().capture_required_assets(
                session,
                run_id=run_id,
                region_id=settings.data_asset_region_id,
                strict=False,
            )
        snapshots = (
            await session.scalars(
                select(DataAssetSnapshot)
                .where(DataAssetSnapshot.run_id == run_id)
                .order_by(DataAssetSnapshot.asset_key)
            )
        ).all()

    assert second.fingerprint == first.fingerprint
    assert [snapshot.version for snapshot in snapshots] == [
        "2022.1",
        "2022.1",
    ]


async def test_strict_snapshot_fails_when_required_asset_is_missing(
    session_factory,
    seeded_assessment_run,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(LookupError, match="required data assets"):
                await DataAssetSnapshotService().capture_required_assets(
                    session,
                    run_id=seeded_assessment_run,
                    region_id=settings.data_asset_region_id,
                    strict=True,
                )
