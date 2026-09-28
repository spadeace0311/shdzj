from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.data_assets.domain import NormalizedRasterData
from app.data_assets.geojson_importer import GeoJsonAssetImporter
from app.data_assets.import_jobs import (
    claim_next_import_job,
    complete_import_job,
    fail_import_job,
    reject_import_job,
)
from app.data_assets.mdb_importer import MdbAssetImporter
from app.data_assets.models import DataAsset, DataAssetImportJob
from app.data_assets.parameter_importer import ParameterFileImporter
from app.data_assets.raster_importer import GeoTiffAssetImporter
from app.data_assets.registry import get_asset_definition
from app.data_assets.service import DataAssetService
from app.data_assets.storage import ManagedFileStore
from app.db import SessionFactory

IMPORTERS = {
    "geojson": GeoJsonAssetImporter(),
    "parameter_file": ParameterFileImporter(),
    "mdb": MdbAssetImporter(),
    "geotiff": GeoTiffAssetImporter(),
}


async def process_import_job(
    session: AsyncSession,
    job: DataAssetImportJob,
) -> None:
    if job.asset_version_id is None:
        raise RuntimeError("data asset import job has no candidate version")
    asset = await session.get(DataAsset, job.asset_id)
    if asset is None:
        raise LookupError("data asset import job references a missing asset")
    definition = get_asset_definition(asset.asset_key)
    importer = IMPORTERS.get(job.file_format)
    if importer is None:
        await fail_import_job(
            session,
            job.id,
            ValueError(f"unsupported import format: {job.file_format}"),
        )
        return
    store = ManagedFileStore(
        settings.data_asset_storage_root,
        max_upload_bytes=settings.data_asset_max_upload_bytes,
    )
    path = store.resolve(job.managed_path)
    service = DataAssetService()
    try:
        async with session.begin_nested():
            normalized = importer.load(path, definition)
            await service.populate_candidate_version(
                session,
                job.asset_version_id,
                normalized,
                {
                    "file_format": job.file_format,
                    "record_count": getattr(normalized, "record_count", None),
                    "source_crs": getattr(normalized, "source_crs", None),
                    "importer": "data-asset-worker",
                },
                source_path=(
                    path if isinstance(normalized, NormalizedRasterData) else None
                ),
            )
            report = await service.validate_version(
                session,
                job.asset_version_id,
                actor=job.requested_by,
            )
    except Exception as error:
        await fail_import_job(session, job.id, error)
        return
    if report.publishable:
        await complete_import_job(session, job.id, job.asset_version_id)
    else:
        await reject_import_job(session, job.id, report)


async def run_worker() -> None:
    while True:
        processed = False
        async with SessionFactory() as session:
            async with session.begin():
                job = await claim_next_import_job(session)
                if job is not None:
                    await process_import_job(session, job)
                    processed = True
        if not processed:
            await asyncio.sleep(settings.data_asset_worker_poll_seconds)


if __name__ == "__main__":
    asyncio.run(run_worker())
