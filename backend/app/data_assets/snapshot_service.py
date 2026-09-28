import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assessment.models import AssessmentRun
from app.config import settings
from app.data_assets.models import DataAssetSnapshot, DataAssetVersion
from app.data_assets.repository import (
    AssetRaster,
    AssetRecord,
    DataAssetRepository,
)
from app.data_assets.required_registry import (
    RequiredAssetRegistry,
    load_required_asset_registry,
)


@dataclass(frozen=True, slots=True)
class DataAssetSnapshotResult:
    snapshot_count: int
    missing_required: tuple[str, ...]
    fingerprint: str


class DataAssetSnapshotService:
    def __init__(
        self,
        *,
        registry: RequiredAssetRegistry | None = None,
        repository: DataAssetRepository | None = None,
    ) -> None:
        self._registry = registry or load_required_asset_registry(
            settings.data_asset_required_registry_path
        )
        self._repository = repository or DataAssetRepository()

    async def capture_required_assets(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        region_id: str,
        strict: bool,
    ) -> DataAssetSnapshotResult:
        if region_id != self._registry.region_id:
            raise ValueError("required asset registry does not match region_id")
        run_exists = await session.scalar(
            select(AssessmentRun.id)
            .where(AssessmentRun.id == run_id)
            .with_for_update()
        )
        if run_exists is None:
            raise LookupError("assessment run not found")
        existing = (
            await session.scalars(
                select(DataAssetSnapshot)
                .where(
                    DataAssetSnapshot.run_id == run_id,
                    DataAssetSnapshot.region_id == region_id,
                )
                .order_by(DataAssetSnapshot.asset_key)
            )
        ).all()
        if existing:
            locked = list(existing)
            locked_keys = {snapshot.asset_key for snapshot in locked}
            missing = tuple(
                sorted(set(self._registry.required) - locked_keys)
            )
            if strict and missing:
                raise LookupError(
                    f"required data assets are missing: {', '.join(missing)}"
                )
            return DataAssetSnapshotResult(
                snapshot_count=len(locked),
                missing_required=missing,
                fingerprint=_fingerprint(locked),
            )

        locked: list[DataAssetSnapshot] = []
        missing: list[str] = []
        assignments = (
            *(
                (asset_key, "required")
                for asset_key in self._registry.required
            ),
            *(
                (asset_key, "optional")
                for asset_key in self._registry.optional
            ),
        )
        now = datetime.now(UTC)
        for asset_key, role in assignments:
            version = await self._repository.get_published_version(
                session,
                asset_key=asset_key,
                region_id=region_id,
            )
            if version is None:
                if role == "required":
                    missing.append(asset_key)
                continue
            locked.append(
                DataAssetSnapshot(
                    run_id=run_id,
                    asset_id=version.asset_id,
                    asset_version_id=version.id,
                    region_id=region_id,
                    asset_key=asset_key,
                    version=version.version,
                    checksum=version.checksum or "",
                    role=role,
                    required=role == "required",
                    created_at=now,
                )
            )
        missing_tuple = tuple(sorted(missing))
        if strict and missing_tuple:
            raise LookupError(
                f"required data assets are missing: {', '.join(missing_tuple)}"
            )
        for snapshot in locked:
            session.add(snapshot)
        await session.flush()
        return DataAssetSnapshotResult(
            snapshot_count=len(locked),
            missing_required=missing_tuple,
            fingerprint=_fingerprint(locked),
        )

    async def get_locked_version(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        asset_key: str,
    ) -> DataAssetVersion | None:
        snapshot = await session.scalar(
            select(DataAssetSnapshot).where(
                DataAssetSnapshot.run_id == run_id,
                DataAssetSnapshot.asset_key == asset_key,
            )
        )
        if snapshot is None:
            return None
        return await session.get(DataAssetVersion, snapshot.asset_version_id)

    async def list_locked_records(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        asset_key: str,
    ) -> list[AssetRecord]:
        version = await self.get_locked_version(
            session,
            run_id=run_id,
            asset_key=asset_key,
        )
        if version is None:
            return []
        return await self._repository.list_records(session, version.id)

    async def get_locked_raster(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        asset_key: str,
    ) -> AssetRaster | None:
        version = await self.get_locked_version(
            session,
            run_id=run_id,
            asset_key=asset_key,
        )
        if version is None:
            return None
        return await self._repository.get_raster(session, version.id)


def _fingerprint(snapshots: list[DataAssetSnapshot]) -> str:
    items = sorted(
        (snapshot.asset_key, snapshot.version, snapshot.checksum)
        for snapshot in snapshots
    )
    payload = json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
