from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.data_assets.snapshot_service import DataAssetSnapshotService
from app.loss.domain import ParameterSet
from app.loss.models_registry import load_parameter_set_from_mapping
from app.loss.region import RegionLossProfile, load_region_loss_profile

if TYPE_CHECKING:
    from app.loss.exposure import ExposureDataset


class LossAssetBridge:
    def __init__(
        self,
        *,
        snapshots: DataAssetSnapshotService,
        profile: RegionLossProfile | None = None,
        list_records=None,
        list_features=None,
    ) -> None:
        self._snapshots = snapshots
        self._profile = profile or load_region_loss_profile(
            settings.loss_region_profile_path
        )
        self._list_records = list_records or snapshots.list_locked_records
        self._list_features = list_features or snapshots.list_locked_records

    async def lock_and_load(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        region_id: str,
    ) -> "ExposureDataset":
        from app.loss.exposure import build_exposure_dataset

        if region_id != self._profile.region_id:
            raise ValueError("loss region profile does not match run region_id")
        snapshot = await self._snapshots.capture_required_assets(
            session,
            run_id=run_id,
            region_id=region_id,
            strict=True,
        )
        keys = self._profile.asset_keys
        city_version = await self._snapshots.get_locked_version(
            session,
            run_id=run_id,
            asset_key=keys.admin_city,
        )
        town_version = await self._snapshots.get_locked_version(
            session,
            run_id=run_id,
            asset_key=keys.admin_town,
        )
        population_version = await self._snapshots.get_locked_version(
            session,
            run_id=run_id,
            asset_key=keys.population_town,
        )
        building_version = await self._snapshots.get_locked_version(
            session,
            run_id=run_id,
            asset_key=keys.building_town,
        )
        if (
            city_version is None
            or town_version is None
            or population_version is None
            or building_version is None
        ):
            raise LookupError("required loss data asset is not locked")
        city = await self._list_records(
            session,
            run_id=run_id,
            asset_key=keys.admin_city,
        )
        towns = await self._list_records(
            session,
            run_id=run_id,
            asset_key=keys.population_town,
        )
        geometries = await self._list_features(
            session,
            run_id=run_id,
            asset_key=keys.admin_town,
        )
        buildings = await self._list_records(
            session,
            run_id=run_id,
            asset_key=keys.building_town,
        )
        return build_exposure_dataset(
            snapshot_checksum=snapshot.fingerprint,
            city=city,
            towns=towns,
            geometries=geometries,
            buildings=buildings,
        )


async def load_locked_parameter_set(
    session: AsyncSession,
    *,
    run_id: UUID,
    profile: RegionLossProfile,
    snapshots: DataAssetSnapshotService | None = None,
) -> ParameterSet:
    snapshot_service = snapshots or DataAssetSnapshotService()
    version = await snapshot_service.get_locked_version(
        session,
        run_id=run_id,
        asset_key=profile.asset_keys.loss_parameters,
    )
    if version is None:
        raise LookupError("loss parameter asset is not locked")
    records = await snapshot_service.list_locked_records(
        session,
        run_id=run_id,
        asset_key=profile.asset_keys.loss_parameters,
    )
    if len(records) != 1:
        raise ValueError("loss parameter asset requires exactly one record")
    return load_parameter_set_from_mapping(
        records[0].properties,
        checksum=version.checksum,
    )
