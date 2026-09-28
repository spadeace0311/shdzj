from app.data_assets.import_jobs import (
    QueueImportRequest,
    claim_next_import_job,
    queue_import_job,
)
from app.data_assets.models import DataAssetImportJob


async def test_import_queue_claim_is_single_consumer(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            job = await queue_import_job(
                session,
                QueueImportRequest(
                    asset_key="shanghai.admin.town",
                    version="2022.1",
                    source_uri="https://example.gov.invalid/town.geojson",
                    license_name=None,
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    change_note="initial import",
                    file_name="town.geojson",
                    file_format="geojson",
                    file_size_bytes=12,
                    checksum="a" * 64,
                    relative_path="aa/aa/aaaaaaaa-aa.geojson",
                    requested_by="tester",
                ),
            )
            job_id = job.id

    async with session_factory() as session:
        async with session.begin():
            claimed = await claim_next_import_job(session)
    async with session_factory() as session:
        async with session.begin():
            second = await claim_next_import_job(session)
            stored = await session.get(DataAssetImportJob, job_id)

    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.asset_version_id is not None
    assert second is None
    assert stored.status == "running"
