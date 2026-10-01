from uuid import uuid4

import pytest

from tests.data_asset_helpers import wait_for_import_job


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _active_region_boundary(session_factory):
    from tests.data_asset_helpers import _with_boundary

    await _with_boundary(session_factory)


async def test_unimported_first_party_asset_is_visible_before_first_import(
    data_asset_client,
) -> None:
    response = await data_asset_client.get(
        "/api/v1/data-assets?region_id=shanghai"
    )

    assert response.status_code == 200
    loss_parameters = next(
        item
        for item in response.json()
        if item["asset_key"] == "shanghai.loss.parameters"
    )
    assert loss_parameters["published_version"] is None
    assert loss_parameters["published_at"] is None
    assert loss_parameters["data_type"] == "parameter"
    assert loss_parameters["is_core"] is True


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
    job_id = response.json()["job_id"]
    version_id = await wait_for_import_job(
        session_factory,
        job_id,
    )
    job_response = await data_asset_client.get(
        f"/api/v1/data-asset-import-jobs/{job_id}"
    )
    assert job_response.status_code == 200
    assert job_response.json() == {
        "job_id": job_id,
        "asset_key": "shanghai.admin.town",
        "version": "2022.1",
        "version_id": str(version_id),
        "status": "completed",
        "error_summary": None,
        "validation_errors": [],
        "validation_warnings": [],
        "statistics": {
            "record_count": 1,
            "column_count": 2,
            "area_crs": "EPSG:32651",
            "projected_area": job_response.json()["statistics"]["projected_area"],
            "coverage_ratio": 1.0,
            "coverage_status": "sufficient",
            "coverage_policy_version": "shanghai-data-asset-coverage-v1",
        },
        "started_at": job_response.json()["started_at"],
        "completed_at": job_response.json()["completed_at"],
        "created_at": job_response.json()["created_at"],
    }

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
    assert published.json()["validation_warnings"] == []
    assert published.json()["statistics"]["coverage_status"] == "sufficient"

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


async def test_rejected_import_job_exposes_validation_diagnostics(
    data_asset_client,
    session_factory,
) -> None:
    response = await data_asset_client.post(
        "/api/v1/data-assets/shanghai.admin.town/import",
        data={
            "version": f"rejected-diagnostics-{uuid4()}",
            "source_uri": "https://example.gov.invalid/town-invalid.geojson",
            "change_note": "invalid import",
        },
        files={
            "file": (
                "town.geojson",
                (
                    '{"type":"FeatureCollection","features":[{"type":"Feature",'
                    '"properties":{"ID":"310115000001"},'
                    '"geometry":{"type":"MultiPolygon","coordinates":'
                    '[[[[121.4,31.1],[121.6,31.1],[121.6,31.4],'
                    '[121.4,31.4],[121.4,31.1]]]]}}]}'
                ).encode(),
                "application/geo+json",
            )
        },
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    await wait_for_import_job(session_factory, job_id)

    job_response = await data_asset_client.get(
        f"/api/v1/data-asset-import-jobs/{job_id}"
    )
    assert job_response.status_code == 200
    payload = job_response.json()
    assert payload["status"] == "rejected"
    assert payload["error_summary"]
    assert any(
        issue["code"] == "required_field_missing"
        for issue in payload["validation_errors"]
    )
    assert payload["statistics"]["record_count"] == 1


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
