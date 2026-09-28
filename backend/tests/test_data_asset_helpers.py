from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.data_assets.import_jobs import QueueImportRequest, queue_import_job
from app.data_assets.models import DataAssetRecord, DataAssetVersion
from tests.data_asset_helpers import (
    ASSESSMENT_OUTBOX_TYPE,
    FIXTURE_ACTOR,
    _cleanup_fixture_data,
    _published_version_has_town_keys,
    _town_codes,
)


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


def _queue_request(version: str, requested_by: str) -> QueueImportRequest:
    return QueueImportRequest(
        asset_key="shanghai.admin.town",
        version=version,
        source_uri="https://example.gov.invalid/town.geojson",
        license_name=None,
        acquired_at=None,
        valid_from=None,
        valid_to=None,
        change_note="data asset helper test",
        file_name="town.geojson",
        file_format="geojson",
        file_size_bytes=1,
        checksum="a" * 64,
        relative_path="aa/aa/aaaaaaaa-aa.geojson",
        requested_by=requested_by,
    )


async def _queue_version(session, version: str, requested_by: str):
    return await queue_import_job(
        session,
        _queue_request(version, requested_by),
    )


def test_shared_data_asset_fixture_functions_are_registered(
    request,
) -> None:
    fixture_manager = request.session._fixturemanager
    registered = set(fixture_manager._arg2fixturedefs)
    assert {
        "candidate_factory",
        "published_population_asset",
        "seeded_assessment_run",
        "data_asset_client",
        "geojson_town_file",
        "seeded_outbox",
        "seeded_imported_version",
    } <= registered


async def test_cleanup_fixture_data_removes_only_fixture_versions(
    session_factory,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            marked = await _queue_version(
                session,
                f"fixture-cleanup-{uuid4()}",
                FIXTURE_ACTOR,
            )
            kept = await _queue_version(
                session,
                f"fixture-cleanup-keep-{uuid4()}",
                "keep-this-version",
            )
            marked_id = marked.asset_version_id
            kept_id = kept.asset_version_id

    await _cleanup_fixture_data(session_factory)

    async with session_factory() as session:
        marked_row = await session.get(DataAssetVersion, marked_id)
        kept_row = await session.get(DataAssetVersion, kept_id)
        if kept_row is not None:
            await session.execute(
                delete(DataAssetVersion).where(DataAssetVersion.id == kept_id)
            )
            await session.commit()

    assert marked_row is None
    assert kept_row is not None


async def test_published_version_has_town_keys_detects_key_set(
    session_factory,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            partial = await _queue_version(
                session,
                f"town-key-partial-{uuid4()}",
                FIXTURE_ACTOR,
            )
            full = await _queue_version(
                session,
                f"town-key-full-{uuid4()}",
                FIXTURE_ACTOR,
            )
            session.add(
                DataAssetRecord(
                    version_id=partial.asset_version_id,
                    row_number=1,
                    business_key="310115000001",
                    properties={},
                )
            )
            for row_number, business_key in enumerate(_town_codes(), start=1):
                session.add(
                    DataAssetRecord(
                        version_id=full.asset_version_id,
                        row_number=row_number,
                        business_key=business_key,
                        properties={},
                    )
                )
            await session.flush()
            assert await _published_version_has_town_keys(
                session,
                partial.asset_version_id,
            ) is False
            assert await _published_version_has_town_keys(
                session,
                full.asset_version_id,
            ) is True

    await _cleanup_fixture_data(session_factory)


async def test_seeded_outbox_uses_assessment_trigger(
    session_factory,
    seeded_outbox,
) -> None:
    from app.events.models import EventLifecycleOutbox

    async with session_factory() as session:
        outbox = await session.get(EventLifecycleOutbox, seeded_outbox.outbox_id)

    assert outbox is not None
    assert outbox.trigger_type == ASSESSMENT_OUTBOX_TYPE
