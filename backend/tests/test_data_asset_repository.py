import pytest

from app.config import settings
from app.data_assets.repository import DataAssetRepository


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def test_repository_lists_published_versions_and_records(
    session_factory,
    published_population_asset,
) -> None:
    repository = DataAssetRepository()
    async with session_factory() as session:
        published = await repository.get_published_version(
            session,
            asset_key="shanghai.population.town",
            region_id=settings.data_asset_region_id,
        )
        versions = await repository.list_versions(
            session,
            asset_key="shanghai.population.town",
            region_id=settings.data_asset_region_id,
        )
        records = await repository.list_records(session, published.id)

    assert published.id == published_population_asset
    assert [version.id for version in versions] == [published_population_asset]
    assert len(records) == 212
    assert records[0].business_key


async def test_repository_returns_none_when_raster_is_not_loaded(
    session_factory,
    seeded_imported_version,
) -> None:
    async with session_factory() as session:
        raster = await DataAssetRepository().get_raster(
            session,
            seeded_imported_version.version_id,
        )
    assert raster is None
