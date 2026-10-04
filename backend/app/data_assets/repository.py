from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_assets.models import (
    DataAsset,
    DataAssetRaster,
    DataAssetRecord,
    DataAssetVersion,
)


@dataclass(frozen=True, slots=True)
class AssetRecord:
    row_number: int
    business_key: str
    properties: dict
    geometry_wkt: str | None


@dataclass(frozen=True, slots=True)
class AssetRaster:
    version_id: UUID
    width: int
    height: int
    srid: int
    checksum: str
    band_manifest: dict
    spatial_extent_wkt: str | None


class DataAssetRepository:
    async def get_asset(
        self,
        session: AsyncSession,
        *,
        asset_key: str,
        region_id: str,
    ) -> DataAsset | None:
        return await session.scalar(
            select(DataAsset).where(
                DataAsset.asset_key == asset_key,
                DataAsset.region_id == region_id,
            )
        )

    async def get_published_version(
        self,
        session: AsyncSession,
        *,
        asset_key: str,
        region_id: str,
    ) -> DataAssetVersion | None:
        return await session.scalar(
            select(DataAssetVersion)
            .join(DataAsset, DataAssetVersion.asset_id == DataAsset.id)
            .where(
                DataAsset.asset_key == asset_key,
                DataAsset.region_id == region_id,
                DataAssetVersion.status == "published",
            )
        )

    async def list_versions(
        self,
        session: AsyncSession,
        *,
        asset_key: str | None = None,
        region_id: str | None = None,
    ) -> list[DataAssetVersion]:
        statement = select(DataAssetVersion).join(
            DataAsset,
            DataAssetVersion.asset_id == DataAsset.id,
        )
        if asset_key is not None:
            statement = statement.where(DataAsset.asset_key == asset_key)
        if region_id is not None:
            statement = statement.where(DataAsset.region_id == region_id)
        statement = statement.order_by(
            DataAssetVersion.created_at,
            DataAssetVersion.id,
        )
        return list((await session.scalars(statement)).all())

    async def list_records(
        self,
        session: AsyncSession,
        version_id: UUID,
    ) -> list[AssetRecord]:
        rows = (
            await session.execute(
                select(
                    DataAssetRecord.row_number,
                    DataAssetRecord.business_key,
                    DataAssetRecord.properties,
                    func.ST_AsText(DataAssetRecord.geom),
                )
                .where(DataAssetRecord.version_id == version_id)
                .order_by(DataAssetRecord.row_number)
            )
        ).all()
        return [
            AssetRecord(
                row_number=row[0],
                business_key=row[1],
                properties=dict(row[2] or {}),
                geometry_wkt=row[3],
            )
            for row in rows
        ]

    async def get_raster(
        self,
        session: AsyncSession,
        version_id: UUID,
    ) -> AssetRaster | None:
        row = (
            await session.execute(
                select(
                    DataAssetRaster.version_id,
                    DataAssetRaster.width,
                    DataAssetRaster.height,
                    DataAssetRaster.srid,
                    DataAssetRaster.checksum,
                    DataAssetRaster.band_manifest,
                    func.ST_AsText(DataAssetRaster.spatial_extent),
                ).where(DataAssetRaster.version_id == version_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return AssetRaster(
            version_id=row[0],
            width=row[1],
            height=row[2],
            srid=row[3],
            checksum=row[4],
            band_manifest=dict(row[5] or {}),
            spatial_extent_wkt=row[6],
        )
