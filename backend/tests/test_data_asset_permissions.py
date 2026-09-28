from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.data_assets.models import DataAssetVersion
from app.main import app

PERMISSION_SEED_ACTOR = "data-asset-permission-seed"


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.mark.parametrize(
    ("role", "status"),
    [
        ("superadmin", 202),
        ("data_maintainer", 202),
        ("data_publisher", 202),
        ("viewer", 403),
        ("group_member", 403),
    ],
)
async def test_import_role_matrix(role: str, status: int, session_factory) -> None:
    version = f"permission-import-{uuid4()}"
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username=f"{role}-user",
        role=role,
        workgroup=None,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/data-assets/shanghai.admin.town/import",
                data={
                    "version": version,
                    "source_uri": "https://example.gov.invalid/town.geojson",
                    "change_note": "initial",
                },
                files={
                    "file": (
                        "town.geojson",
                        b'{"type":"FeatureCollection","features":[]}',
                        "application/geo+json",
                    )
                },
            )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous

    assert response.status_code == status
    if status == 202:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(DataAssetVersion).where(
                        DataAssetVersion.imported_by == f"{role}-user"
                    )
                )


@pytest.mark.parametrize(
    ("role", "status"),
    [
        ("superadmin", 200),
        ("data_publisher", 200),
        ("data_maintainer", 403),
        ("viewer", 403),
    ],
)
async def test_publish_role_matrix(
    role: str,
    status: int,
    session_factory,
) -> None:
    version_id = await _seed_publish_candidate(session_factory)
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username=f"{role}-user",
        role=role,
        workgroup=None,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                f"/api/v1/data-asset-versions/{version_id}/publish",
                json={"reason": "approved"},
            )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous

    try:
        assert response.status_code == status
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(DataAssetVersion).where(
                        DataAssetVersion.imported_by == PERMISSION_SEED_ACTOR
                    )
                )


async def _seed_publish_candidate(session_factory):
    from app.data_assets.import_jobs import QueueImportRequest, queue_import_job
    from app.data_assets.service import DataAssetService
    from tests.data_asset_helpers import _populate, _town_records

    async with session_factory() as session:
        async with session.begin():
            job = await queue_import_job(
                session,
                QueueImportRequest(
                    asset_key="shanghai.admin.town",
                    version=f"permission-publish-seed-{uuid4()}",
                    source_uri="https://example.gov.invalid/town.geojson",
                    license_name=None,
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    change_note="permission publish seed",
                    file_name="town.geojson",
                    file_format="geojson",
                    file_size_bytes=1,
                    checksum="a" * 64,
                    relative_path="aa/aa/permission-publish-seed.geojson",
                    requested_by=PERMISSION_SEED_ACTOR,
                ),
            )
            await _populate(
                session,
                job.asset_version_id,
                _town_records(),
                importer="geojson",
            )
            await DataAssetService().validate_version(
                session,
                job.asset_version_id,
                actor=PERMISSION_SEED_ACTOR,
            )
            return job.asset_version_id
