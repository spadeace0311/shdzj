from tests.data_asset_helpers import wait_for_import_job


async def test_import_validate_publish_and_list(
    data_asset_client,
    geojson_town_file,
    session_factory,
) -> None:
    await _delete_admin_town_2022_1(session_factory)
    response = await data_asset_client.post(
        "/api/v1/data-assets/shanghai.admin.town/import",
        data={
            "version": "2022.1",
            "source_uri": "https://example.gov.invalid/town.geojson",
            "change_note": "initial import",
        },
        files={
            "file": (
                "town.geojson",
                geojson_town_file.read_bytes(),
                "application/geo+json",
            )
        },
    )
    assert response.status_code == 202
    version_id = await wait_for_import_job(
        session_factory,
        response.json()["job_id"],
    )

    validated = await data_asset_client.post(
        f"/api/v1/data-asset-versions/{version_id}/validate"
    )
    assert validated.status_code == 200
    assert validated.json()["status"] == "validated"

    published = await data_asset_client.post(
        f"/api/v1/data-asset-versions/{version_id}/publish",
        json={"reason": "verified against 2022 base data"},
    )
    assert published.status_code == 200
    assert published.json()["status"] == "published"

    try:
        assets = await data_asset_client.get(
            "/api/v1/data-assets?region_id=shanghai"
        )
        assert assets.status_code == 200
        assert {item["region_id"] for item in assets.json()} == {"shanghai"}
        town = next(item for item in assets.json() if item["asset_key"] == "shanghai.admin.town")
        assert town["published_version"] == "2022.1"
    finally:
        await _delete_admin_town_2022_1(session_factory)


async def _delete_admin_town_2022_1(session_factory) -> None:
    from sqlalchemy import delete, select

    from app.data_assets.models import DataAsset, DataAssetVersion

    async with session_factory() as session:
        async with session.begin():
            asset_ids = (
                await session.scalars(
                    select(DataAsset.id).where(
                        DataAsset.asset_key == "shanghai.admin.town",
                        DataAsset.region_id == "shanghai",
                    )
                )
            ).all()
            if asset_ids:
                await session.execute(
                    delete(DataAssetVersion).where(
                        DataAssetVersion.asset_id.in_(asset_ids),
                        DataAssetVersion.version == "2022.1",
                    )
                )
