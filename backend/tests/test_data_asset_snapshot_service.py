import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.data_assets.locks import lock_data_asset_catalog
from app.data_assets.models import (
    DataAsset,
    DataAssetSnapshot,
    DataAssetVersion,
)
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


async def test_snapshot_capture_waits_for_the_shared_catalog_lock(
    session_factory,
    seeded_assessment_run,
    published_population_asset,
) -> None:
    captured = asyncio.Event()

    async def capture() -> None:
        async with session_factory() as session:
            async with session.begin():
                await DataAssetSnapshotService().capture_required_assets(
                    session,
                    run_id=seeded_assessment_run,
                    region_id=settings.data_asset_region_id,
                    strict=False,
                )
        captured.set()

    async with session_factory() as lock_session:
        async with lock_session.begin():
            await lock_data_asset_catalog(lock_session)
            task = asyncio.create_task(capture())
            await asyncio.sleep(0.2)
            assert not captured.is_set()
            assert not task.done()

    await asyncio.wait_for(task, timeout=5)
    assert captured.is_set()


async def test_snapshot_insert_requires_published_version_and_matching_metadata(
    session_factory,
    seeded_assessment_run,
    candidate_factory,
) -> None:
    candidate_id = await candidate_factory("snapshot-contract")
    async with session_factory() as session:
        async with session.begin():
            candidate = await session.get(DataAssetVersion, candidate_id)
            asset = await session.get(DataAsset, candidate.asset_id)
            with pytest.raises(
                IntegrityError,
                match="data_asset_snapshot_requires_published_version",
            ):
                session.add(
                    DataAssetSnapshot(
                        run_id=seeded_assessment_run,
                        asset_id=asset.id,
                        region_id=asset.region_id,
                        asset_version_id=candidate.id,
                        asset_key=asset.asset_key,
                        version=candidate.version,
                        checksum=candidate.checksum,
                        role="required",
                        required=True,
                    )
                )
                await session.flush()


async def test_snapshot_insert_rejects_mismatched_catalog_metadata(
    session_factory,
    seeded_assessment_run,
    published_population_asset,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            version = await session.get(
                DataAssetVersion,
                published_population_asset,
            )
            asset = await session.get(DataAsset, version.asset_id)
            with pytest.raises(
                IntegrityError,
                match="data_asset_snapshot_metadata_mismatch",
            ):
                session.add(
                    DataAssetSnapshot(
                        run_id=seeded_assessment_run,
                        asset_id=asset.id,
                        region_id=asset.region_id,
                        asset_version_id=version.id,
                        asset_key=f"{asset.asset_key}.wrong",
                        version=version.version,
                        checksum=version.checksum,
                        role="required",
                        required=True,
                    )
                )
                await session.flush()
