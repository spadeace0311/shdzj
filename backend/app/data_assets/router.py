from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.router import require_role
from app.config import settings
from app.data_assets.domain import (
    AssetDataType,
    AssetVersionStatus,
    ImportJobStatus,
    SourceFormat,
    ValidationReport,
    validate_source_uri,
)
from app.data_assets.importer import UnsupportedImportFormat
from app.data_assets.import_jobs import QueueImportRequest, queue_import_job, sanitize_error
from app.data_assets.registry import get_asset_definition
from app.data_assets.schemas import (
    AssetSummaryResponse,
    AssetVersionResponse,
    ImportAcceptedResponse,
    LifecycleActionRequest,
    ValidationReportResponse,
)
from app.data_assets.service import (
    AssetVersionDetailView,
    AssetVersionView,
    DataAssetService,
)
from app.data_assets.storage import ManagedFileStore
from app.db import get_session

router = APIRouter(prefix="/api/v1", tags=["data-assets"])

_DATA_READ_ROLES = (
    "superadmin",
    "data_maintainer",
    "data_publisher",
    "group_leader",
    "group_deputy",
    "group_member",
    "viewer",
)
_DATA_WRITE_ROLES = ("superadmin", "data_maintainer", "data_publisher")
_DATA_PUBLISH_ROLES = ("superadmin", "data_publisher")


def get_data_asset_service() -> DataAssetService:
    return DataAssetService()


def source_format_for_file(
    file_name: str,
    definition,
) -> SourceFormat:
    suffix = Path(file_name).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return SourceFormat.PARAMETER_FILE
    if suffix in {".json"} and definition.data_type == AssetDataType.PARAMETER:
        return SourceFormat.PARAMETER_FILE
    if suffix in {".geojson", ".json"}:
        return SourceFormat.GEOJSON
    if suffix in {".mdb", ".accdb"}:
        return SourceFormat.MDB
    if suffix in {".tif", ".tiff", ".geotiff"}:
        return SourceFormat.GEOTIFF
    raise UnsupportedImportFormat(
        f"unsupported data asset upload format for file: {Path(file_name).name}"
    )


def map_validation_report_response(
    report: ValidationReport,
) -> ValidationReportResponse:
    return ValidationReportResponse.model_validate(asdict(report))


def map_asset_version_response(
    view: AssetVersionView,
) -> AssetVersionResponse:
    payload = asdict(view)
    payload["id"] = str(payload.pop("version_id"))
    return AssetVersionResponse.model_validate(payload)


def map_asset_version_detail_response(
    view: AssetVersionDetailView,
) -> AssetVersionResponse:
    return map_asset_version_response(view.summary)


def _map_data_asset_error(error: Exception) -> HTTPException:
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail="data_asset_not_found")
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail="Insufficient permissions")
    if isinstance(error, UnsupportedImportFormat):
        return HTTPException(status_code=422, detail=sanitize_error(error))
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=sanitize_error(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=409, detail=sanitize_error(error))
    if isinstance(error, SQLAlchemyError):
        return HTTPException(
            status_code=503,
            detail="data asset storage is unavailable",
        )
    raise error


@router.get("/data-assets", response_model=list[AssetSummaryResponse])
async def list_data_assets(
    region_id: str = settings.data_asset_region_id,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    _current_user: object = Depends(require_role(*_DATA_READ_ROLES)),
) -> list[AssetSummaryResponse]:
    if not region_id.strip():
        raise HTTPException(status_code=422, detail="region_id must not be blank")
    try:
        views = await service.list_assets(session, region_id=region_id)
    except (KeyError, LookupError, PermissionError, ValueError, SQLAlchemyError) as exc:
        raise _map_data_asset_error(exc) from exc
    return [AssetSummaryResponse.model_validate(asdict(view)) for view in views]


@router.post(
    "/data-assets/{asset_key}/import",
    response_model=ImportAcceptedResponse,
    status_code=202,
)
async def import_data_asset(
    asset_key: str,
    version: str = Form(...),
    source_uri: str = Form(...),
    license_name: str | None = Form(default=None),
    change_note: str = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    current_user: object = Depends(require_role(*_DATA_WRITE_ROLES)),
) -> ImportAcceptedResponse:
    del service
    try:
        definition = get_asset_definition(asset_key)
        normalized_source_uri = validate_source_uri(source_uri)
        upload_name = file.filename or "upload"
        file_format = source_format_for_file(upload_name, definition)
        store = ManagedFileStore(
            settings.data_asset_storage_root,
            max_upload_bytes=settings.data_asset_max_upload_bytes,
        )
        stored = store.store_upload(
            file.file,
            file_name=upload_name,
        )
        async with session.begin():
            job = await queue_import_job(
                session,
                QueueImportRequest(
                    asset_key=asset_key,
                    version=version,
                    source_uri=normalized_source_uri,
                    license_name=license_name,
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    change_note=change_note,
                    file_name=stored.file_name,
                    file_format=file_format.value,
                    file_size_bytes=stored.size_bytes,
                    checksum=stored.checksum,
                    relative_path=stored.relative_path,
                    requested_by=current_user.username,
                ),
            )
            if job.asset_version_id is None:
                raise RuntimeError("import job has no candidate version")
            return ImportAcceptedResponse(
                job_id=str(job.id),
                asset_key=asset_key,
                version=version,
                status=ImportJobStatus(job.status),
                version_id=str(job.asset_version_id),
            )
    except (
        KeyError,
        LookupError,
        PermissionError,
        UnsupportedImportFormat,
        ValueError,
        SQLAlchemyError,
    ) as exc:
        raise _map_data_asset_error(exc) from exc


@router.get("/data-asset-versions", response_model=list[AssetVersionResponse])
async def list_data_asset_versions(
    asset_key: str | None = None,
    region_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    _current_user: object = Depends(require_role(*_DATA_READ_ROLES)),
) -> list[AssetVersionResponse]:
    try:
        views = await service.list_versions(
            session,
            asset_key=asset_key,
            region_id=region_id,
        )
    except (KeyError, LookupError, PermissionError, ValueError, SQLAlchemyError) as exc:
        raise _map_data_asset_error(exc) from exc
    return [map_asset_version_response(view) for view in views]


@router.get("/data-asset-versions/{version_id}", response_model=AssetVersionResponse)
async def get_data_asset_version(
    version_id: UUID,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    _current_user: object = Depends(require_role(*_DATA_READ_ROLES)),
) -> AssetVersionResponse:
    try:
        view = await service.get_version_detail(session, version_id)
    except (KeyError, LookupError, PermissionError, ValueError, SQLAlchemyError) as exc:
        raise _map_data_asset_error(exc) from exc
    return map_asset_version_detail_response(view)


@router.post(
    "/data-asset-versions/{version_id}/validate",
    response_model=ValidationReportResponse,
)
async def validate_data_asset_version(
    version_id: UUID,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    current_user: object = Depends(require_role(*_DATA_WRITE_ROLES)),
) -> ValidationReportResponse:
    try:
        async with session.begin():
            report = await service.validate_version(
                session,
                version_id,
                actor=current_user.username,
            )
    except (KeyError, LookupError, PermissionError, ValueError, SQLAlchemyError) as exc:
        raise _map_data_asset_error(exc) from exc
    return map_validation_report_response(report)


@router.post(
    "/data-asset-versions/{version_id}/publish",
    response_model=AssetVersionResponse,
)
async def publish_data_asset_version(
    version_id: UUID,
    request: LifecycleActionRequest,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    current_user: object = Depends(require_role(*_DATA_PUBLISH_ROLES)),
) -> AssetVersionResponse:
    try:
        async with session.begin():
            version = await service.publish_version(
                session,
                version_id,
                current_user.username,
                request.reason,
            )
            return await _version_response_for_model(session, version)
    except (KeyError, LookupError, PermissionError, ValueError, SQLAlchemyError) as exc:
        raise _map_data_asset_error(exc) from exc


@router.post(
    "/data-asset-versions/{version_id}/retire",
    response_model=AssetVersionResponse,
)
async def retire_data_asset_version(
    version_id: UUID,
    request: LifecycleActionRequest,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    current_user: object = Depends(require_role(*_DATA_PUBLISH_ROLES)),
) -> AssetVersionResponse:
    try:
        async with session.begin():
            version = await service.retire_version(
                session,
                version_id,
                current_user.username,
                request.reason,
            )
            return await _version_response_for_model(session, version)
    except (KeyError, LookupError, PermissionError, ValueError, SQLAlchemyError) as exc:
        raise _map_data_asset_error(exc) from exc


@router.post(
    "/data-asset-versions/{version_id}/rollback",
    response_model=AssetVersionResponse,
)
async def rollback_data_asset_version(
    version_id: UUID,
    request: LifecycleActionRequest,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    current_user: object = Depends(require_role(*_DATA_PUBLISH_ROLES)),
) -> AssetVersionResponse:
    try:
        async with session.begin():
            version = await service.rollback_version(
                session,
                version_id,
                current_user.username,
                request.reason,
            )
            return await _version_response_for_model(session, version)
    except (KeyError, LookupError, PermissionError, ValueError, SQLAlchemyError) as exc:
        raise _map_data_asset_error(exc) from exc


async def _version_response_for_model(session, version) -> AssetVersionResponse:
    from app.data_assets.models import DataAsset

    asset = await session.get(DataAsset, version.asset_id)
    if asset is None:
        raise LookupError("data asset version references a missing asset")
    return map_asset_version_response(
        AssetVersionView(
            version_id=version.id,
            asset_key=asset.asset_key,
            region_id=asset.region_id,
            version=version.version,
            status=AssetVersionStatus(version.status),
            source_uri=version.source_uri,
            license_name=version.license_name,
            acquired_at=version.acquired_at,
            valid_from=version.valid_from,
            valid_to=version.valid_to,
            quality_grade=version.quality_grade,
            change_note=version.change_note,
            schema_summary=dict(version.schema_summary),
            record_count=version.record_count,
            checksum=version.checksum or "",
            imported_by=version.imported_by or "",
            reviewed_by=version.reviewed_by,
            imported_at=version.imported_at or version.created_at,
            validated_at=version.validated_at,
            published_at=version.published_at,
            retired_at=version.retired_at,
            validation_errors=(),
            validation_warnings=(),
        )
    )
