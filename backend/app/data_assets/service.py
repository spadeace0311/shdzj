from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from uuid import UUID

from geoalchemy2.elements import WKTElement
from shapely import wkt as shapely_wkt
from shapely.geometry import box
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.data_assets.coverage import (
    RegionCoveragePolicy,
    evaluate_region_coverage,
    load_policy_region_profile,
    load_region_coverage_policy,
)
from app.data_assets.domain import (
    AggregateFieldTolerance,
    AssetDataType,
    AssetVersionStatus,
    NormalizedAssetData,
    NormalizedRasterData,
    NormalizedRecord,
    NormalizedTableData,
    ValidationIssue,
    ValidationReport,
)
from app.data_assets.models import (
    DataAsset,
    DataAssetAuditLog,
    DataAssetImportJob,
    DataAssetRaster,
    DataAssetRecord,
    DataAssetVersion,
)
from app.data_assets.locks import lock_data_asset_catalog
from app.data_assets.raster_repository import save_raster_version
from app.data_assets.registry import FIRST_PARTY_ASSETS, get_asset_definition
from app.data_assets.repository import DataAssetRepository
from app.data_assets.validators import DataAssetValidator
from app.regions.models import RegionBoundary


@dataclass(frozen=True, slots=True)
class AssetSummaryView:
    asset_key: str
    region_id: str
    name: str
    data_type: str
    spatial_granularity: str
    responsibility_unit: str
    update_interval_days: int
    is_core: bool
    published_version: str | None
    published_at: datetime | None
    update_due_at: datetime | None
    is_update_overdue: bool


@dataclass(frozen=True, slots=True)
class AssetVersionView:
    version_id: UUID
    asset_key: str
    region_id: str
    version: str
    status: AssetVersionStatus
    source_uri: str
    source_crs: str
    license_name: str | None
    acquired_at: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None
    quality_grade: str | None
    change_note: str | None
    schema_summary: dict
    record_count: int
    checksum: str
    imported_by: str
    reviewed_by: str | None
    imported_at: datetime
    validated_at: datetime | None
    published_at: datetime | None
    retired_at: datetime | None
    validation_errors: tuple[ValidationIssue, ...]
    validation_warnings: tuple[ValidationIssue, ...]
    statistics: dict


@dataclass(frozen=True, slots=True)
class AssetVersionDetailView:
    summary: AssetVersionView


@dataclass(frozen=True, slots=True)
class ImportJobView:
    job_id: UUID
    asset_key: str
    version: str
    version_id: UUID
    status: str
    error_summary: str | None
    validation_errors: tuple[ValidationIssue, ...]
    validation_warnings: tuple[ValidationIssue, ...]
    statistics: dict
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AggregateCheckEvaluation:
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()


ALLOWED_TRANSITIONS: dict[AssetVersionStatus, frozenset[AssetVersionStatus]] = {
    AssetVersionStatus.IMPORTED: frozenset(
        {AssetVersionStatus.VALIDATED, AssetVersionStatus.REJECTED}
    ),
    AssetVersionStatus.VALIDATED: frozenset({AssetVersionStatus.PUBLISHED}),
    AssetVersionStatus.PUBLISHED: frozenset({AssetVersionStatus.RETIRED}),
    AssetVersionStatus.RETIRED: frozenset({AssetVersionStatus.PUBLISHED}),
    AssetVersionStatus.REJECTED: frozenset(),
}

_ADMIN_TOWN_DEPENDENTS = {
    "shanghai.population.town",
    "shanghai.building.town",
}


class DataAssetDecisionError(ValueError):
    def __init__(self, issue: ValidationIssue) -> None:
        self.issue = issue
        super().__init__(issue.message)


class DataAssetService:
    def __init__(
        self,
        *,
        coverage_policy: RegionCoveragePolicy | None = None,
    ) -> None:
        self._repository = DataAssetRepository()
        self._validator = DataAssetValidator()
        self._coverage_policy = coverage_policy

    async def list_assets(
        self,
        session: AsyncSession,
        *,
        region_id: str | None = None,
    ) -> list[AssetSummaryView]:
        statement = select(DataAsset)
        if region_id is not None:
            statement = statement.where(DataAsset.region_id == region_id)
        statement = statement.order_by(DataAsset.asset_key)
        stored_assets = list((await session.scalars(statement)).all())
        definitions = [
            definition
            for definition in FIRST_PARTY_ASSETS
            if region_id is None or definition.region_id == region_id
        ]
        summary_inputs: list[
            tuple[
                str,
                str,
                str,
                str,
                str,
                str,
                int,
                bool,
            ]
        ] = []
        for definition in definitions:
            summary_inputs.append(
                (
                    definition.asset_key,
                    definition.region_id,
                    definition.name,
                    definition.data_type.value,
                    definition.spatial_granularity,
                    definition.responsibility_unit,
                    definition.update_interval_days,
                    definition.is_core,
                )
            )
        for asset in stored_assets:
            if (asset.region_id, asset.asset_key) in {
                (definition.region_id, definition.asset_key)
                for definition in definitions
            }:
                continue
            summary_inputs.append(
                (
                    asset.asset_key,
                    asset.region_id,
                    asset.name,
                    asset.data_type,
                    asset.spatial_granularity,
                    asset.responsibility_unit,
                    asset.update_interval_days,
                    asset.is_core,
                )
            )
        summary_inputs.sort(key=lambda item: (item[1], item[0]))
        views: list[AssetSummaryView] = []
        now = datetime.now(UTC)
        for (
            asset_key,
            asset_region_id,
            name,
            data_type,
            spatial_granularity,
            responsibility_unit,
            update_interval_days,
            is_core,
        ) in summary_inputs:
            published = await self._repository.get_published_version(
                session,
                asset_key=asset_key,
                region_id=asset_region_id,
            )
            published_at = published.published_at if published else None
            update_due_at = (
                published_at + timedelta(days=update_interval_days)
                if published_at is not None
                else None
            )
            views.append(
                AssetSummaryView(
                    asset_key=asset_key,
                    region_id=asset_region_id,
                    name=name,
                    data_type=data_type,
                    spatial_granularity=spatial_granularity,
                    responsibility_unit=responsibility_unit,
                    update_interval_days=update_interval_days,
                    is_core=is_core,
                    published_version=published.version if published else None,
                    published_at=published_at,
                    update_due_at=update_due_at,
                    is_update_overdue=(
                        update_due_at is not None and update_due_at <= now
                    ),
                )
            )
        return views

    async def list_versions(
        self,
        session: AsyncSession,
        *,
        asset_key: str | None = None,
        region_id: str | None = None,
    ) -> list[AssetVersionView]:
        versions = await self._repository.list_versions(
            session,
            asset_key=asset_key,
            region_id=region_id,
        )
        return [
            await self._version_view(
                session,
                version,
                await session.get(DataAsset, version.asset_id),
            )
            for version in versions
        ]

    async def get_version_detail(
        self,
        session: AsyncSession,
        version_id: UUID,
    ) -> AssetVersionDetailView:
        version = await session.get(DataAssetVersion, version_id)
        if version is None:
            raise LookupError("data asset version not found")
        asset = await session.get(DataAsset, version.asset_id)
        if asset is None:
            raise LookupError("data asset version references a missing asset")
        return AssetVersionDetailView(
            summary=await self._version_view(session, version, asset)
        )

    async def get_import_job(
        self,
        session: AsyncSession,
        job_id: UUID,
    ) -> ImportJobView:
        job = await session.get(DataAssetImportJob, job_id)
        if job is None:
            raise LookupError("data asset import job not found")
        asset = await session.get(DataAsset, job.asset_id)
        version = await session.get(DataAssetVersion, job.asset_version_id)
        if asset is None or version is None:
            raise LookupError(
                "data asset import job references missing catalog data"
            )
        return ImportJobView(
            job_id=job.id,
            asset_key=asset.asset_key,
            version=version.version,
            version_id=version.id,
            status=job.status,
            error_summary=job.error_summary,
            validation_errors=_stored_validation_issues(
                job.validation_errors,
                "error",
            ),
            validation_warnings=_stored_validation_issues(
                job.validation_warnings,
                "warning",
            ),
            statistics=dict(job.statistics),
            started_at=job.started_at,
            completed_at=job.completed_at,
            created_at=job.created_at,
        )

    async def populate_candidate_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        normalized: NormalizedAssetData,
        schema_summary: dict,
        *,
        source_path: Path | None = None,
    ) -> DataAssetVersion:
        version = await session.get(
            DataAssetVersion,
            version_id,
            with_for_update=True,
        )
        if version is None:
            raise LookupError("data asset version not found")
        if version.status != "imported":
            raise ValueError("only imported versions can be populated")
        asset = await session.get(
            DataAsset,
            version.asset_id,
            with_for_update=True,
        )
        if asset is None:
            raise LookupError("data asset version references a missing asset")

        if isinstance(normalized, NormalizedRasterData):
            if source_path is None:
                raise ValueError("source_path is required for raster data")
            existing = await session.scalar(
                select(DataAssetRaster.id).where(
                    DataAssetRaster.version_id == version_id
                )
            )
            if existing is not None:
                stored_canonical_checksum = await session.scalar(
                    select(DataAssetRaster.checksum).where(
                        DataAssetRaster.version_id == version_id
                    )
                )
                if (
                    stored_canonical_checksum is None
                    or version.checksum != stored_canonical_checksum
                ):
                    raise ValueError(
                        "version checksum does not match persisted raster payload"
                    )
                source_checksum = hashlib.sha256(
                    source_path.read_bytes()
                ).hexdigest()
                stored_source_checksum = (version.schema_summary or {}).get(
                    "source_checksum"
                )
                if stored_source_checksum is None:
                    version.schema_summary = {
                        **dict(version.schema_summary),
                        "source_checksum": source_checksum,
                    }
                elif stored_source_checksum != source_checksum:
                    raise ValueError(
                        "raster source payload checksum does not match "
                        "the imported candidate"
                    )
            else:
                await save_raster_version(
                    session,
                    version_id,
                    source_path,
                    normalized,
                )
            version.schema_summary = {
                **dict(version.schema_summary),
                **dict(schema_summary),
                "source_checksum": hashlib.sha256(
                    source_path.read_bytes()
                ).hexdigest(),
            }
            version.record_count = 0
            version.source_crs = normalized.source_crs
            version.spatial_extent = _bounds_wkt_element(
                normalized.spatial_extent
            )
        else:
            if source_path is not None:
                raise ValueError("source_path is only valid for raster data")
            existing_count = await session.scalar(
                select(func.count())
                .select_from(DataAssetRecord)
                .where(DataAssetRecord.version_id == version_id)
            )
            checksum = _table_checksum(normalized)
            if existing_count:
                stored_checksum = (version.schema_summary or {}).get(
                    "normalized_checksum"
                )
                if stored_checksum != checksum:
                    raise ValueError(
                        "normalized data checksum does not match existing "
                        "candidate version"
                    )
                return version
            session.add_all(
                [
                    DataAssetRecord(
                        version_id=version_id,
                        row_number=record.row_number,
                        business_key=record.business_key,
                        properties=dict(record.properties),
                        geom=(
                            WKTElement(record.geometry_wkt, srid=4326)
                            if record.geometry_wkt is not None
                            else None
                        ),
                    )
                    for record in normalized.records
                ]
            )
            summary = dict(schema_summary)
            summary["normalized_checksum"] = checksum
            summary["columns"] = list(normalized.columns)
            version.schema_summary = summary
            version.record_count = normalized.record_count
            version.spatial_extent = _bounds_wkt_element(
                normalized.spatial_extent
            )
            await session.flush()
            persisted_records = await self._repository.list_records(
                session,
                version_id,
            )
            version.checksum = _table_checksum(
                NormalizedTableData(
                    columns=normalized.columns,
                    records=tuple(
                        NormalizedRecord(
                            row_number=record.row_number,
                            business_key=record.business_key,
                            properties=dict(record.properties),
                            geometry_wkt=record.geometry_wkt,
                        )
                        for record in persisted_records
                    ),
                    source_crs=normalized.source_crs,
                    spatial_extent=None,
                )
            )
            version.updated_at = datetime.now(UTC)
            await session.flush()
            return version

        version.updated_at = datetime.now(UTC)
        await session.flush()
        return version

    async def validate_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        *,
        actor: str = "system",
    ) -> ValidationReport:
        version = await session.get(
            DataAssetVersion,
            version_id,
            with_for_update=True,
        )
        if version is None:
            raise LookupError("data asset version not found")
        if version.status not in {"imported", "validated"}:
            raise ValueError("data asset version is immutable")
        asset = await session.get(DataAsset, version.asset_id)
        if asset is None:
            raise LookupError("data asset version references a missing asset")
        definition = get_asset_definition(asset.asset_key)
        normalized = await self._load_normalized(session, version, definition)
        report = self._validator.validate(definition, normalized)
        report = ValidationReport(
            version_id=str(version.id),
            status=report.status,
            errors=report.errors,
            warnings=report.warnings,
            statistics=report.statistics,
            checked_at=report.checked_at,
        )
        dependency_issue = await self._admin_town_key_set_issue(
            session,
            version,
            asset,
        )
        if dependency_issue is not None:
            report = ValidationReport(
                version_id=report.version_id,
                status=AssetVersionStatus.REJECTED,
                errors=(*report.errors, dependency_issue),
                warnings=report.warnings,
                statistics=report.statistics,
                checked_at=report.checked_at,
            )
        report = await self._apply_region_coverage(
            session,
            definition,
            normalized,
            report,
        )
        report = await self._apply_aggregate_checks(
            session,
            definition,
            normalized,
            version,
            report,
        )

        now = datetime.now(UTC)
        version.status = report.status.value
        version.validated_at = now
        version.updated_at = now
        version.schema_summary = {
            **dict(version.schema_summary),
            "validation_statistics": dict(report.statistics),
        }
        session.add(
            DataAssetAuditLog(
                asset_id=asset.id,
                version_id=version.id,
                action="validate",
                actor=actor,
                reason=None,
                details={
                    "status": report.status.value,
                    "error_codes": [issue.code for issue in report.errors],
                    "warning_codes": [issue.code for issue in report.warnings],
                },
                created_at=now,
            )
        )
        await session.flush()
        return report

    async def update_candidate_metadata(
        self,
        session: AsyncSession,
        version_id: UUID,
        *,
        change_note: str,
        actor: str,
    ) -> DataAssetVersion:
        version = await session.get(
            DataAssetVersion,
            version_id,
            with_for_update=True,
        )
        if version is None:
            raise LookupError("data asset version not found")
        if version.status != "imported":
            raise ValueError("published data asset version is immutable")
        now = datetime.now(UTC)
        version.change_note = change_note
        version.updated_at = now
        session.add(
            DataAssetAuditLog(
                asset_id=version.asset_id,
                version_id=version.id,
                action="update_candidate_metadata",
                actor=actor,
                reason=change_note,
                details={"change_note": change_note},
                created_at=now,
            )
        )
        await session.flush()
        return version

    async def publish_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        actor: str,
        reason: str,
    ) -> DataAssetVersion:
        await lock_data_asset_catalog(session)
        target = await session.get(
            DataAssetVersion,
            version_id,
            with_for_update=True,
        )
        if target is None:
            raise LookupError("data asset version not found")
        asset = await session.get(
            DataAsset,
            target.asset_id,
            with_for_update=True,
        )
        if asset is None:
            raise LookupError("data asset version references a missing asset")
        current_status = AssetVersionStatus(target.status)
        target_status = AssetVersionStatus.PUBLISHED
        if target_status not in ALLOWED_TRANSITIONS[current_status]:
            raise ValueError(
                f"invalid data asset version status transition: "
                f"{current_status.value} -> {target_status.value}"
            )
        issue = await self._admin_town_key_set_issue(session, target, asset)
        if issue is not None:
            raise DataAssetDecisionError(issue)
        return await self._publish_target(
            session,
            asset,
            target,
            actor,
            reason,
        )

    async def retire_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        actor: str,
        reason: str,
    ) -> DataAssetVersion:
        await lock_data_asset_catalog(session)
        target = await session.get(
            DataAssetVersion,
            version_id,
            with_for_update=True,
        )
        if target is None:
            raise LookupError("data asset version not found")
        asset = await session.get(
            DataAsset,
            target.asset_id,
            with_for_update=True,
        )
        if asset is None:
            raise LookupError("data asset version references a missing asset")
        current_status = AssetVersionStatus(target.status)
        target_status = AssetVersionStatus.RETIRED
        if target_status not in ALLOWED_TRANSITIONS[current_status]:
            raise ValueError(
                f"invalid data asset version status transition: "
                f"{current_status.value} -> {target_status.value}"
            )
        now = datetime.now(UTC)
        target.status = target_status.value
        target.retired_at = now
        target.updated_at = now
        session.add(
            DataAssetAuditLog(
                asset_id=asset.id,
                version_id=target.id,
                action="retire",
                actor=actor,
                reason=reason,
                details={
                    "old_version_id": str(target.id),
                    "new_version_id": None,
                },
                created_at=now,
            )
        )
        await session.flush()
        return target

    async def rollback_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        actor: str,
        reason: str,
    ) -> DataAssetVersion:
        await lock_data_asset_catalog(session)
        target = await session.get(
            DataAssetVersion,
            version_id,
            with_for_update=True,
        )
        if target is None:
            raise LookupError("data asset version not found")
        asset = await session.get(
            DataAsset,
            target.asset_id,
            with_for_update=True,
        )
        if asset is None:
            raise LookupError("data asset version references a missing asset")
        current_status = AssetVersionStatus(target.status)
        target_status = AssetVersionStatus.PUBLISHED
        if target_status not in ALLOWED_TRANSITIONS[current_status]:
            raise ValueError(
                f"invalid data asset version status transition: "
                f"{current_status.value} -> {target_status.value}"
            )
        issue = await self._admin_town_key_set_issue(session, target, asset)
        if issue is not None:
            raise DataAssetDecisionError(issue)
        return await self._publish_target(
            session,
            asset,
            target,
            actor,
            reason,
        )

    async def _publish_target(
        self,
        session: AsyncSession,
        asset: DataAsset,
        target: DataAssetVersion,
        actor: str,
        reason: str,
    ) -> DataAssetVersion:
        current = await session.scalar(
            select(DataAssetVersion)
            .where(
                DataAssetVersion.asset_id == asset.id,
                DataAssetVersion.status == "published",
                DataAssetVersion.id != target.id,
            )
            .with_for_update()
        )
        now = datetime.now(UTC)
        if current is not None:
            current.status = AssetVersionStatus.RETIRED.value
            current.retired_at = now
            current.updated_at = now
            session.add(
                DataAssetAuditLog(
                    asset_id=asset.id,
                    version_id=current.id,
                    action="retire",
                    actor=actor,
                    reason=reason,
                    details={
                        "old_version_id": str(current.id),
                        "new_version_id": str(target.id),
                    },
                    created_at=now,
                )
            )
            await session.flush()
        target.status = AssetVersionStatus.PUBLISHED.value
        target.published_at = now
        target.reviewed_by = actor
        target.updated_at = now
        session.add(
            DataAssetAuditLog(
                asset_id=asset.id,
                version_id=target.id,
                action="publish",
                actor=actor,
                reason=reason,
                details={
                    "old_version_id": (
                        str(current.id) if current is not None else None
                    ),
                    "new_version_id": str(target.id),
                },
                created_at=now,
            )
        )
        await session.flush()
        return target

    async def _load_normalized(
        self,
        session: AsyncSession,
        version: DataAssetVersion,
        definition,
    ) -> NormalizedAssetData:
        if definition.data_type == AssetDataType.RASTER:
            raster = await self._repository.get_raster(session, version.id)
            if raster is None:
                raise ValueError("data asset raster is not loaded")
            manifest = dict(raster.band_manifest)
            return NormalizedRasterData(
                width=raster.width,
                height=raster.height,
                srid=raster.srid,
                source_crs=version.source_crs,
                band_count=int(manifest.get("band_count", 1)),
                dtype=str(manifest.get("dtype", "float32")),
                nodata=manifest.get("nodata"),
                resolution_x=float(manifest["resolution_x"]),
                resolution_y=float(manifest["resolution_y"]),
                native_spatial_extent=tuple(
                    manifest.get(
                        "native_bounds",
                        _wkt_to_bounds(raster.spatial_extent_wkt),
                    )
                ),
                spatial_extent=_wkt_to_bounds(raster.spatial_extent_wkt),
            )

        records = await self._repository.list_records(session, version.id)
        columns = tuple(
            sorted({key for record in records for key in record.properties})
        )
        return NormalizedTableData(
            columns=columns,
            records=tuple(
                NormalizedRecord(
                    row_number=record.row_number,
                    business_key=record.business_key,
                    properties=dict(record.properties),
                    geometry_wkt=record.geometry_wkt,
                )
                for record in records
            ),
            source_crs=version.source_crs,
            spatial_extent=await self._version_extent(session, version.id),
        )

    async def _version_extent(
        self,
        session: AsyncSession,
        version_id: UUID,
    ) -> tuple[float, float, float, float] | None:
        wkt = await session.scalar(
            select(func.ST_AsText(DataAssetVersion.spatial_extent)).where(
                DataAssetVersion.id == version_id
            )
        )
        return _wkt_to_bounds(wkt)

    async def _apply_region_coverage(
        self,
        session: AsyncSession,
        definition,
        normalized: NormalizedAssetData,
        report: ValidationReport,
    ) -> ValidationReport:
        try:
            policy = self._coverage_policy or load_region_coverage_policy(
                settings.data_asset_coverage_policy_path
            )
            if policy.region_id != definition.region_id:
                raise ValueError(
                    "coverage policy region_id does not match the asset region"
                )
            profile = load_policy_region_profile(policy)
        except Exception as exc:
            statistics = dict(report.statistics)
            statistics.update(
                {
                    "area_crs": None,
                    "projected_area": None,
                    "coverage_ratio": None,
                    "coverage_status": "configuration_error",
                    "coverage_policy_version": None,
                }
            )
            return ValidationReport(
                version_id=report.version_id,
                status=AssetVersionStatus.REJECTED,
                errors=(
                    *report.errors,
                    ValidationIssue(
                        severity="error",
                        code="coverage_configuration_invalid",
                        message=f"coverage configuration is invalid: {exc}",
                    ),
                ),
                warnings=report.warnings,
                statistics=statistics,
                checked_at=report.checked_at,
            )

        boundary_wkt = await session.scalar(
            select(func.ST_AsText(RegionBoundary.geom))
            .where(RegionBoundary.is_active.is_(True))
            .limit(1)
        )
        coverage = evaluate_region_coverage(
            policy=policy,
            profile=profile,
            asset_key=definition.asset_key,
            normalized=normalized,
            boundary_wkt=boundary_wkt,
        )
        errors = (*report.errors, *coverage.errors)
        warnings = (*report.warnings, *coverage.warnings)
        statistics = dict(report.statistics)
        statistics.update(coverage.statistics)
        return ValidationReport(
            version_id=report.version_id,
            status=(
                AssetVersionStatus.REJECTED
                if errors
                else AssetVersionStatus.VALIDATED
            ),
            errors=errors,
            warnings=warnings,
            statistics=statistics,
            checked_at=report.checked_at,
        )

    async def _apply_aggregate_checks(
        self,
        session: AsyncSession,
        definition,
        normalized: NormalizedAssetData,
        version: DataAssetVersion,
        report: ValidationReport,
    ) -> ValidationReport:
        if definition.contract.aggregate_of is None:
            return report
        if definition.contract.aggregate_of == definition.asset_key:
            self_issue = ValidationIssue(
                severity="error",
                code="aggregate_difference_exceeded",
                message="aggregate_of must reference a coarser parent asset",
            )
            return ValidationReport(
                version_id=str(version.id),
                status=AssetVersionStatus.REJECTED,
                errors=(*report.errors, self_issue),
                warnings=report.warnings,
                statistics=report.statistics,
                checked_at=report.checked_at,
            )
        parent = await self._repository.get_published_version(
            session,
            asset_key=definition.contract.aggregate_of,
            region_id=definition.region_id,
        )
        aggregate_checks: list[dict[str, object]] = []
        if parent is None:
            aggregate_checks.append(
                {
                    "parent_asset_key": definition.contract.aggregate_of,
                    "status": "missing_parent",
                }
            )
        elif isinstance(normalized, NormalizedTableData):
            parent_records = await self._repository.list_records(
                session,
                parent.id,
            )
            aggregate_checks.extend(
                build_aggregate_checks(
                    definition,
                    normalized,
                    parent_records,
                )
            )

        evaluation = evaluate_aggregate_checks(aggregate_checks)
        errors = [*report.errors, *evaluation.errors]
        warnings = [*report.warnings, *evaluation.warnings]

        statistics = dict(report.statistics)
        statistics["aggregate_checks"] = aggregate_checks
        status = (
            AssetVersionStatus.REJECTED
            if errors
            else AssetVersionStatus.VALIDATED
        )
        return ValidationReport(
            version_id=str(version.id),
            status=status,
            errors=tuple(errors),
            warnings=tuple(warnings),
            statistics=statistics,
            checked_at=report.checked_at,
        )

    async def _admin_town_key_set_issue(
        self,
        session: AsyncSession,
        version: DataAssetVersion,
        asset: DataAsset,
    ) -> ValidationIssue | None:
        if asset.asset_key not in _ADMIN_TOWN_DEPENDENTS:
            return None
        admin_town = await self._repository.get_published_version(
            session,
            asset_key="shanghai.admin.town",
            region_id=asset.region_id,
        )
        if admin_town is None:
            return ValidationIssue(
                severity="error",
                code="business_key_set_mismatch",
                message=(
                    "dependent data asset requires a published "
                    "shanghai.admin.town version"
                ),
            )
        target_keys = {
            record.business_key
            for record in await self._repository.list_records(
                session,
                version.id,
            )
        }
        admin_keys = {
            record.business_key
            for record in await self._repository.list_records(
                session,
                admin_town.id,
            )
        }
        if target_keys != admin_keys:
            missing = sorted(admin_keys - target_keys)
            extra = sorted(target_keys - admin_keys)
            return ValidationIssue(
                severity="error",
                code="business_key_set_mismatch",
                message=(
                    "dependent asset business key set does not match "
                    "shanghai.admin.town"
                    + (f"; missing={missing}" if missing else "")
                    + (f"; extra={extra}" if extra else "")
                ),
            )
        return None

    @staticmethod
    async def _version_view(
        session: AsyncSession,
        version: DataAssetVersion,
        asset: DataAsset | None,
    ) -> AssetVersionView:
        if asset is None:
            raise LookupError("data asset version references a missing asset")
        import_job = await session.scalar(
            select(DataAssetImportJob)
            .where(DataAssetImportJob.asset_version_id == version.id)
            .order_by(
                DataAssetImportJob.completed_at.desc().nullslast(),
                DataAssetImportJob.created_at.desc(),
            )
            .limit(1)
        )
        validation_errors = _stored_validation_issues(
            import_job.validation_errors if import_job is not None else [],
            "error",
        )
        validation_warnings = _stored_validation_issues(
            import_job.validation_warnings if import_job is not None else [],
            "warning",
        )
        return AssetVersionView(
            version_id=version.id,
            asset_key=asset.asset_key,
            region_id=asset.region_id,
            version=version.version,
            status=AssetVersionStatus(version.status),
            source_uri=version.source_uri,
            source_crs=version.source_crs,
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
            validation_errors=validation_errors,
            validation_warnings=validation_warnings,
            statistics=(
                dict(import_job.statistics)
                if import_job is not None
                else {}
            ),
        )


def _bounds_wkt_element(
    extent: tuple[float, float, float, float] | None,
) -> WKTElement | None:
    if extent is None:
        return None
    if len(extent) != 4 or not all(isfinite(float(value)) for value in extent):
        raise ValueError("spatial extent is invalid")
    return WKTElement(box(*extent).wkt, srid=4326)


def _wkt_to_bounds(
    wkt: str | None,
) -> tuple[float, float, float, float] | None:
    if wkt is None:
        return None
    geometry = shapely_wkt.loads(wkt)
    return tuple(float(value) for value in geometry.bounds)


def _table_checksum(normalized: NormalizedTableData) -> str:
    payload = {
        "columns": list(normalized.columns),
        "records": [
            {
                "row_number": record.row_number,
                "business_key": record.business_key,
                "properties": record.properties,
                "geometry_wkt": record.geometry_wkt,
            }
            for record in normalized.records
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_table_checksum(normalized: NormalizedTableData) -> str:
    """Return the canonical content checksum for normalized table data."""
    return _table_checksum(normalized)


def build_aggregate_checks(
    definition,
    normalized: NormalizedTableData,
    parent_records,
) -> list[dict[str, object]]:
    tolerance_by_field = {
        tolerance.field_name.upper(): tolerance
        for tolerance in definition.contract.aggregate_tolerances
    }
    checks: list[dict[str, object]] = []
    for field in definition.contract.fields:
        if field.python_type not in {"number", "integer"}:
            continue
        tolerance = tolerance_by_field.get(
            field.name.upper(),
            AggregateFieldTolerance(
                field_name=field.name,
                warning_threshold=0.001,
                error_threshold=0.005,
                basis="strict default: 0.1% warning, 0.5% error",
            ),
        )
        child_values = [
            _matching_numeric_value(record.properties, field.name)
            for record in normalized.records
        ]
        parent_values = [
            _matching_numeric_value(record.properties, field.name)
            for record in parent_records
        ]
        check: dict[str, object] = {
            "field_name": field.name,
            "child_total": None,
            "parent_total": None,
            "absolute_difference": None,
            "relative_difference": None,
            "warning_threshold": tolerance.warning_threshold,
            "error_threshold": tolerance.error_threshold,
            "tolerance_basis": tolerance.basis,
        }
        if not any(value is not None for value in child_values) and not any(
            value is not None for value in parent_values
        ):
            check["status"] = "not_applicable"
            checks.append(check)
            continue
        if any(value is None for value in child_values) or any(
            value is None for value in parent_values
        ):
            check["status"] = "incomplete"
            checks.append(check)
            continue

        child_total = sum(
            value for value in child_values if value is not None
        )
        parent_total = sum(
            value for value in parent_values if value is not None
        )
        absolute_difference = abs(child_total - parent_total)
        relative_difference = (
            absolute_difference / abs(parent_total)
            if parent_total
            else (
                0.0
                if absolute_difference == 0
                else float("inf")
            )
        )
        check.update(
            {
                "child_total": child_total,
                "parent_total": parent_total,
                "absolute_difference": absolute_difference,
                "relative_difference": relative_difference,
                "status": (
                    "error"
                    if relative_difference > tolerance.error_threshold
                    else (
                        "warning"
                        if relative_difference > tolerance.warning_threshold
                        else "within_tolerance"
                    )
                ),
            }
        )
        checks.append(check)
    return checks


def evaluate_aggregate_checks(
    checks: list[dict[str, object]],
) -> AggregateCheckEvaluation:
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    for check in checks:
        status = str(check.get("status", "incomplete"))
        field_name = (
            str(check["field_name"])
            if check.get("field_name") is not None
            else None
        )
        if status == "missing_parent":
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="aggregate_difference_exceeded",
                    message="aggregate parent asset is not published",
                )
            )
        elif status == "incomplete":
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="aggregate_values_incomplete",
                    message=(
                        "aggregate comparison was not performed because "
                        "one or more values are null"
                    ),
                    field_name=field_name,
                )
            )
        elif status == "error":
            warnings_or_errors = errors
            warnings_or_errors.append(
                ValidationIssue(
                    severity="error",
                    code="aggregate_difference_exceeded",
                    message=(
                        "aggregate difference exceeds the allowed "
                        f"threshold for {field_name}"
                    ),
                    field_name=field_name,
                )
            )
        elif status == "warning":
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="aggregate_difference_exceeded",
                    message=(
                        "aggregate difference exceeds the warning "
                        f"threshold for {field_name}"
                    ),
                    field_name=field_name,
                )
            )
    return AggregateCheckEvaluation(
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _matching_numeric_value(
    properties: dict,
    field_name: str,
) -> float | None:
    for key, value in properties.items():
        if str(key).lower() == field_name.lower() and value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _stored_validation_issues(
    payloads: list[dict],
    severity: str,
) -> tuple[ValidationIssue, ...]:
    return tuple(
        ValidationIssue(
            severity=severity,
            code=str(payload.get("code", "unknown")),
            message=str(payload.get("message", "")),
            row_number=payload.get("row_number"),
            field_name=payload.get("field_name"),
        )
        for payload in payloads
    )
