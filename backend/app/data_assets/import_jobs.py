from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_assets.domain import ValidationReport, validate_source_uri
from app.data_assets.models import (
    DataAsset,
    DataAssetAuditLog,
    DataAssetImportJob,
    DataAssetVersion,
)
from app.data_assets.registry import get_asset_definition


@dataclass(frozen=True, slots=True)
class QueueImportRequest:
    asset_key: str
    version: str
    source_uri: str
    license_name: str | None
    acquired_at: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None
    change_note: str
    file_name: str
    file_format: str
    file_size_bytes: int
    checksum: str
    relative_path: str
    requested_by: str


async def queue_import_job(
    session: AsyncSession,
    request: QueueImportRequest,
) -> DataAssetImportJob:
    source_uri = validate_source_uri(request.source_uri)
    definition = get_asset_definition(request.asset_key)
    asset = await _ensure_asset(session, definition)
    existing_version = await session.scalar(
        select(DataAssetVersion)
        .join(DataAsset, DataAssetVersion.asset_id == DataAsset.id)
        .where(
            DataAsset.asset_key == definition.asset_key,
            DataAsset.region_id == definition.region_id,
            DataAssetVersion.version == request.version,
        )
        .with_for_update()
    )
    if existing_version is not None:
        raise ValueError("data asset version already exists")
    now = datetime.now(UTC)
    version = DataAssetVersion(
        asset_id=asset.id,
        version=request.version,
        status="imported",
        source_uri=source_uri,
        license_name=request.license_name,
        acquired_at=request.acquired_at,
        valid_from=request.valid_from,
        valid_to=request.valid_to,
        quality_grade=None,
        change_note=request.change_note,
        schema_summary={},
        record_count=0,
        spatial_extent=None,
        source_crs=definition.contract.source_crs,
        checksum=request.checksum,
        managed_path=request.relative_path,
        imported_by=request.requested_by,
        reviewed_by=None,
        imported_at=now,
        validated_at=None,
        published_at=None,
        retired_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(version)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError("data asset version already exists") from exc
    job = DataAssetImportJob(
        asset_id=asset.id,
        asset_version_id=version.id,
        file_name=request.file_name,
        file_format=request.file_format,
        source_uri=source_uri,
        file_size_bytes=request.file_size_bytes,
        raw_checksum=request.checksum,
        managed_path=request.relative_path,
        status="queued",
        validation_errors=[],
        validation_warnings=[],
        statistics={},
        error_summary=None,
        requested_by=request.requested_by,
        started_at=None,
        completed_at=None,
        created_at=now,
    )
    session.add(job)
    await session.flush()
    session.add(
        DataAssetAuditLog(
            asset_id=asset.id,
            version_id=version.id,
            action="import",
            actor=request.requested_by,
            reason=request.change_note,
            details={
                "file_name": request.file_name,
                "file_format": request.file_format,
                "import_job_id": str(job.id),
            },
            created_at=now,
        )
    )
    return job


async def claim_next_import_job(
    session: AsyncSession,
) -> DataAssetImportJob | None:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('data-asset-import-worker'))"))
    job = await session.scalar(
        select(DataAssetImportJob)
        .where(DataAssetImportJob.status == "queued")
        .order_by(DataAssetImportJob.created_at, DataAssetImportJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return None
    job.status = "running"
    job.started_at = datetime.now(UTC)
    return job


async def complete_import_job(
    session: AsyncSession,
    job_id: UUID,
    version_id: UUID,
) -> None:
    job = await session.get(DataAssetImportJob, job_id, with_for_update=True)
    if job is None:
        raise LookupError("data asset import job not found")
    job.asset_version_id = version_id
    job.status = "completed"
    job.completed_at = datetime.now(UTC)


async def reject_import_job(
    session: AsyncSession,
    job_id: UUID,
    report: ValidationReport,
) -> None:
    job = await session.get(DataAssetImportJob, job_id, with_for_update=True)
    if job is None:
        raise LookupError("data asset import job not found")
    job.status = "rejected"
    job.validation_errors = [
        {
            "code": issue.code,
            "message": issue.message,
            "row_number": issue.row_number,
            "field_name": issue.field_name,
        }
        for issue in report.errors
    ]
    job.validation_warnings = [
        {
            "code": issue.code,
            "message": issue.message,
            "row_number": issue.row_number,
            "field_name": issue.field_name,
        }
        for issue in report.warnings
    ]
    job.error_summary = "; ".join(issue.message for issue in report.errors)[:1000]
    job.completed_at = datetime.now(UTC)
    await _reject_candidate_version(session, job.asset_version_id)
    await append_import_audit(
        session,
        job,
        action="import_rejected",
        details={"error_codes": [issue.code for issue in report.errors]},
    )


async def fail_import_job(
    session: AsyncSession,
    job_id: UUID,
    error: Exception,
) -> None:
    job = await session.get(DataAssetImportJob, job_id, with_for_update=True)
    if job is None:
        raise LookupError("data asset import job not found")
    job.status = "failed"
    job.error_summary = sanitize_error(error)[:1000]
    job.completed_at = datetime.now(UTC)
    await _reject_candidate_version(session, job.asset_version_id)
    await append_import_audit(
        session,
        job,
        action="import_failed",
        details={"error_category": type(error).__name__},
    )


async def append_import_audit(
    session: AsyncSession,
    job: DataAssetImportJob,
    *,
    action: str,
    details: dict,
) -> None:
    session.add(
        DataAssetAuditLog(
            asset_id=job.asset_id,
            version_id=job.asset_version_id,
            action=action,
            actor=job.requested_by,
            reason=None,
            details=details,
            created_at=datetime.now(UTC),
        )
    )


def sanitize_error(error: Exception) -> str:
    message = " ".join(str(error).replace("\x00", " ").split())
    if not message:
        return type(error).__name__
    patterns = (
        r"(?i)\b(?:driver|dbq|database|user|uid|token|api[_-]?key|apikey|"
        r"password|passwd|pwd|secret|access[_-]?token)\b\s*[:=]\s*[^\s,;]+",
        r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/]+:[^\s@/]+@\S+",
        (
            r"(?i)\b(?:postgres(?:ql)?(?:\+asyncpg)?|mysql|mssql|odbc|redis|"
            r"rediss|mongodb(?:\+srv)?|amqp|amqps|oracle|sqlite)://\S+"
        ),
        r"(?i)\b[A-Za-z]:\\[^\s;]+",
    )
    for pattern in patterns:
        message = re.sub(pattern, "[redacted]", message)
    return message[:1000]


async def _reject_candidate_version(
    session: AsyncSession,
    version_id: UUID | None,
) -> None:
    if version_id is None:
        return
    version = await session.get(
        DataAssetVersion,
        version_id,
        with_for_update=True,
    )
    if version is not None and version.status == "imported":
        version.status = "rejected"
        version.updated_at = datetime.now(UTC)


async def _ensure_asset(session: AsyncSession, definition) -> DataAsset:
    asset = await session.scalar(
        select(DataAsset).where(
            DataAsset.asset_key == definition.asset_key,
            DataAsset.region_id == definition.region_id,
        ).with_for_update()
    )
    if asset is not None:
        return asset
    asset = DataAsset(
        asset_key=definition.asset_key,
        region_id=definition.region_id,
        name=definition.name,
        data_type=definition.data_type.value,
        spatial_granularity=definition.spatial_granularity,
        responsibility_unit=definition.responsibility_unit,
        update_interval_days=definition.update_interval_days,
        is_core=definition.is_core,
        contract={
            "business_key_fields": list(definition.contract.business_key_fields),
            "fields": [
                {
                    "name": field.name,
                    "python_type": field.python_type,
                    "required": field.required,
                    "nonnegative": field.nonnegative,
                    "minimum": field.minimum,
                    "maximum": field.maximum,
                }
                for field in definition.contract.fields
            ],
            "geometry_type": definition.contract.geometry_type,
            "source_crs": definition.contract.source_crs,
            "aggregate_of": definition.contract.aggregate_of,
            "expected_record_count": definition.contract.expected_record_count,
            "excluded_business_keys": list(
                definition.contract.excluded_business_keys
            ),
            "exclusion_reason": definition.contract.exclusion_reason,
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(asset)
    await session.flush()
    return asset
