import asyncio

import pytest
from sqlalchemy import select

from app.data_assets.import_jobs import fail_import_job
from app.data_assets.models import (
    DataAssetAuditLog,
    DataAssetImportJob,
    DataAssetVersion,
)
from app.data_assets.service import DataAssetService


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def test_candidate_validation_publish_and_single_published_version(
    session_factory,
    candidate_factory,
) -> None:
    service = DataAssetService()
    first_id = await candidate_factory("2022.1")
    second_id = await candidate_factory("2022.2")

    async with session_factory() as session:
        async with session.begin():
            first = await service.validate_version(session, first_id)
            assert first.publishable
            await service.publish_version(session, first_id, "publisher", "initial")
            second = await service.validate_version(session, second_id)
            assert second.publishable
            await service.publish_version(session, second_id, "publisher", "annual update")
            rows = (
                await session.scalars(
                    select(DataAssetVersion).where(
                        DataAssetVersion.status == "published"
                    )
                )
            ).all()
            audit_rows = (await session.scalars(select(DataAssetAuditLog))).all()

    assert [str(row.id) for row in rows] == [str(second_id)]
    assert [row.action for row in audit_rows] == [
        "import",
        "import",
        "validate",
        "publish",
        "validate",
        "retire",
        "publish",
    ]


async def test_failed_import_writes_failed_job_and_audit(
    session_factory,
    candidate_factory,
) -> None:
    version_id = await candidate_factory("2022.3")
    async with session_factory() as session:
        async with session.begin():
            job = await session.scalar(
                select(DataAssetImportJob).where(
                    DataAssetImportJob.asset_version_id == version_id
                )
            )
            await fail_import_job(session, job.id, ValueError("synthetic parse failure"))

    async with session_factory() as session:
        failed_job = await session.get(DataAssetImportJob, job.id)
        audit_actions = (
            await session.scalars(
                select(DataAssetAuditLog).where(
                    DataAssetAuditLog.version_id == version_id
                )
            )
        ).all()
    assert failed_job.status == "failed"
    assert failed_job.error_summary == "synthetic parse failure"
    assert [row.action for row in audit_actions] == ["import", "import_failed"]


async def test_published_version_is_immutable_and_rollback_republishes(
    session_factory,
    candidate_factory,
) -> None:
    service = DataAssetService()
    first_id = await candidate_factory("2022.1")
    second_id = await candidate_factory("2022.2")
    async with session_factory() as session:
        async with session.begin():
            await service.validate_version(session, first_id)
            await service.publish_version(session, first_id, "publisher", "initial")
            await service.validate_version(session, second_id)
            await service.publish_version(session, second_id, "publisher", "update")
            await service.rollback_version(session, first_id, "publisher", "bad update")
            with pytest.raises(ValueError, match="transition"):
                await service.publish_version(session, first_id, "publisher", "duplicate")

    async with session_factory() as session:
        async with session.begin():
            first = await session.get(DataAssetVersion, first_id)

    assert first.status == "published"


async def test_published_version_rejects_metadata_mutation(
    session_factory,
    candidate_factory,
) -> None:
    service = DataAssetService()
    version_id = await candidate_factory("2022.1")
    async with session_factory() as session:
        async with session.begin():
            await service.validate_version(session, version_id)
            await service.publish_version(session, version_id, "publisher", "initial")
            with pytest.raises(ValueError, match="immutable"):
                await service.update_candidate_metadata(
                    session,
                    version_id,
                    change_note="changed after publication",
                    actor="publisher",
                )


async def test_concurrent_publish_keeps_single_published_version(
    session_factory,
    candidate_factory,
) -> None:
    service = DataAssetService()
    first_id = await candidate_factory("2022.1")
    second_id = await candidate_factory("2022.2")
    async with session_factory() as session:
        async with session.begin():
            await service.validate_version(session, first_id)
            await service.validate_version(session, second_id)

    async def publish(version_id):
        async with session_factory() as session:
            async with session.begin():
                await DataAssetService().publish_version(
                    session,
                    version_id,
                    "publisher",
                    "concurrent update",
                )

    await asyncio.gather(publish(first_id), publish(second_id))

    async with session_factory() as session:
        published = (
            await session.scalars(
                select(DataAssetVersion).where(
                    DataAssetVersion.status == "published"
                )
            )
        ).all()
    assert len(published) == 1
