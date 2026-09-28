from __future__ import annotations

import hashlib
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

from app.data_assets.domain import (
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
    DataAssetRaster,
    DataAssetRecord,
    DataAssetVersion,
)
from app.data_assets.raster_repository import save_raster_version
from app.data_assets.registry import get_asset_definition
from app.data_assets.repository import DataAssetRepository
from app.data_assets.validators import DataAssetValidator


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


@dataclass(frozen=True, slots=True)
class AssetVersionDetailView:
    summary: AssetVersionView


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


class DataAssetService:
    def __init__(self) -> None:
        self._repository = DataAssetRepository()
        self._validator = DataAssetValidator()

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
        assets = (await session.scalars(statement)).all()
        views: list[AssetSummaryView] = []
        now = datetime.now(UTC)
        for asset in assets:
            published = await self._repository.get_published_version(
                session,
                asset_key=asset.asset_key,
                region_id=asset.region_id,
            )
            published_at = published.published_at if published else None
            update_due_at = (
                published_at + timedelta(days=asset.update_interval_days)
                if published_at is not None
                else None
            )
            views.append(
                AssetSummaryView(
                    asset_key=asset.asset_key,
                    region_id=asset.region_id,
                    name=asset.name,
                    data_type=asset.data_type,
                    spatial_granularity=asset.spatial_granularity,
                    responsibility_unit=asset.responsibility_unit,
                    update_interval_days=asset.update_interval_days,
                    is_core=asset.is_core,
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
            self._version_view(
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
            summary=self._version_view(version, asset)
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
                actual_checksum = hashlib.sha256(
                    source_path.read_bytes()
                ).hexdigest()
                if version.checksum != actual_checksum:
                    raise ValueError(
                        "version checksum does not match raster payload"
                    )
            else:
                await save_raster_version(
                    session,
                    version_id,
                    source_path,
                    normalized,
                )
            version.schema_summary = dict(schema_summary)
            version.record_count = 0
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
            if existing_count:
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
            version.schema_summary = dict(schema_summary)
            version.record_count = normalized.record_count
            version.spatial_extent = _bounds_wkt_element(
                normalized.spatial_extent
            )

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
        await self._ensure_admin_town_key_set(session, target, asset)
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
        await self._ensure_admin_town_key_set(session, target, asset)
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
                band_count=int(manifest.get("band_count", 1)),
                dtype=str(manifest.get("dtype", "float32")),
                nodata=manifest.get("nodata"),
                resolution_x=float(manifest["resolution_x"]),
                resolution_y=float(manifest["resolution_y"]),
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
            for field in definition.contract.fields:
                if field.python_type not in {"number", "integer"}:
                    continue
                child_total = sum(
                    float(record.properties.get(field.name) or 0)
                    for record in normalized.records
                )
                parent_total = sum(
                    float(record.properties.get(field.name) or 0)
                    for record in parent_records
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
                aggregate_checks.append(
                    {
                        "field_name": field.name,
                        "child_total": child_total,
                        "parent_total": parent_total,
                        "absolute_difference": absolute_difference,
                        "relative_difference": relative_difference,
                    }
                )

        errors = list(report.errors)
        warnings = list(report.warnings)
        for check in aggregate_checks:
            relative = check.get("relative_difference")
            if relative is None:
                warnings.append(
                    ValidationIssue(
                        severity="warning",
                        code="aggregate_difference_exceeded",
                        message="aggregate parent asset is not published",
                    )
                )
            elif relative > 0.005:
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="aggregate_difference_exceeded",
                        message=(
                            "aggregate difference exceeds the allowed "
                            f"threshold for {check.get('field_name')}"
                        ),
                        field_name=str(check.get("field_name")),
                    )
                )
            elif relative > 0.001:
                warnings.append(
                    ValidationIssue(
                        severity="warning",
                        code="aggregate_difference_exceeded",
                        message=(
                            "aggregate difference exceeds the warning "
                            f"threshold for {check.get('field_name')}"
                        ),
                        field_name=str(check.get("field_name")),
                    )
                )

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

    async def _ensure_admin_town_key_set(
        self,
        session: AsyncSession,
        version: DataAssetVersion,
        asset: DataAsset,
    ) -> None:
        if asset.asset_key not in _ADMIN_TOWN_DEPENDENTS:
            return
        admin_town = await self._repository.get_published_version(
            session,
            asset_key="shanghai.admin.town",
            region_id=asset.region_id,
        )
        if admin_town is None:
            return
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
            raise ValueError(
                "business_key_set_mismatch: dependent asset keys must match "
                "shanghai.admin.town"
                + (f"; missing={missing}" if missing else "")
                + (f"; extra={extra}" if extra else "")
            )

    @staticmethod
    def _version_view(
        version: DataAssetVersion,
        asset: DataAsset | None,
    ) -> AssetVersionView:
        if asset is None:
            raise LookupError("data asset version references a missing asset")
        return AssetVersionView(
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
