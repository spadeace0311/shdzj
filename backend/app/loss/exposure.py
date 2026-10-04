from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.assessment.models import AssessmentRun
from app.config import settings
from app.data_assets.repository import AssetRecord
from app.loss.asset_bridge import LossAssetBridge
from app.loss.domain import BuildingStructure
from app.loss.region import RegionLossProfile, load_region_loss_profile


@dataclass(frozen=True, slots=True)
class BuildingExposure:
    town_code: str
    structure: BuildingStructure
    era: str | None
    area_m2: float


@dataclass(frozen=True, slots=True)
class TownExposure:
    town_code: str
    county_code: str
    town_name: str
    population_total: float
    geometry_wkt: str
    buildings: tuple[BuildingExposure, ...]


@dataclass(frozen=True, slots=True)
class CityExposure:
    area_code: str
    area_name: str


@dataclass(frozen=True, slots=True)
class ExposureDataset:
    snapshot_checksum: str
    city: CityExposure
    towns: tuple[TownExposure, ...]


def _properties(row: AssetRecord | Mapping[str, object]) -> dict[str, object]:
    if isinstance(row, Mapping):
        return dict(row)
    if isinstance(row, AssetRecord):
        return dict(row.properties)
    value = getattr(row, "properties", None)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("exposure row must be an AssetRecord or a mapping")


def _geometry_wkt(row: AssetRecord | Mapping[str, object]) -> str | None:
    if isinstance(row, AssetRecord):
        value = row.geometry_wkt
    elif isinstance(row, Mapping):
        value = row.get("geometry_wkt")
    else:
        value = getattr(row, "geometry_wkt", None)
    return str(value) if value is not None else None


def _business_key(
    row: AssetRecord | Mapping[str, object],
    *keys: str,
) -> str:
    properties = _properties(row)
    for key in keys:
        value = properties.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ValueError(f"exposure row requires one of: {', '.join(keys)}")


def _number(properties: Mapping[str, object], *keys: str) -> float:
    for key in keys:
        value = properties.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be numeric") from exc
    raise ValueError(f"exposure row requires one of: {', '.join(keys)}")


def _required_text(row: AssetRecord | Mapping[str, object], *keys: str) -> str:
    properties = _properties(row)
    for key in keys:
        value = properties.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ValueError(f"exposure row requires one of: {', '.join(keys)}")


def _optional_text(row: AssetRecord | Mapping[str, object], *keys: str) -> str | None:
    properties = _properties(row)
    for key in keys:
        value = properties.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _county_code(row: AssetRecord | Mapping[str, object], town_code: str) -> str:
    explicit = _optional_text(row, "county_code")
    if explicit is not None:
        return explicit
    return town_code[:9]


def _building_areas(
    row: AssetRecord | Mapping[str, object],
) -> tuple[tuple[BuildingStructure, float], ...]:
    properties = _properties(row)
    if "structure" in properties:
        return (
            (
                BuildingStructure(str(properties["structure"])),
                _number(properties, "area_m2"),
            ),
        )
    return (
        (BuildingStructure.HIGH_RISE, _number(properties, "HIGH_RISE")),
        (BuildingStructure.RC_FRAME, _number(properties, "RCFRAME")),
        (BuildingStructure.MASONRY, _number(properties, "BRICK_STRUCTURE")),
        (BuildingStructure.SINGLE_STOREY, _number(properties, "SINGLE_AREA")),
        (BuildingStructure.OTHER, _number(properties, "OTHER_STRUCTURE")),
    )


def build_exposure_dataset(
    *,
    snapshot_checksum: str,
    city: Sequence[AssetRecord | Mapping[str, object]],
    towns: Sequence[AssetRecord | Mapping[str, object]],
    geometries: Sequence[AssetRecord | Mapping[str, object]],
    buildings: Sequence[AssetRecord | Mapping[str, object]],
) -> ExposureDataset:
    if len(snapshot_checksum) != 64:
        raise ValueError("snapshot checksum must be a SHA-256 digest")
    if len(city) != 1:
        raise ValueError("city asset requires exactly one record")
    city_record = city[0]
    city_exposure = CityExposure(
        area_code=_business_key(city_record, "city_code", "ID", "AREA_CODE"),
        area_name=_required_text(city_record, "city_name", "NAME", "name"),
    )

    town_rows = {_business_key(row, "town_code", "ID", "TOWN_CODE"): row for row in towns}
    geometry_rows = {
        _business_key(row, "town_code", "ID", "TOWN_CODE"): _geometry_wkt(row)
        for row in geometries
    }
    if len(town_rows) != len(towns):
        raise ValueError("duplicate town code in population asset")
    if len(geometry_rows) != len(geometries):
        raise ValueError("duplicate town code in boundary asset")
    if set(town_rows) != set(geometry_rows):
        raise ValueError("town population and boundary business keys do not match")
    if any(value is None or not value.strip() for value in geometry_rows.values()):
        raise ValueError("town geometry must not be empty")

    buildings_by_town: dict[str, list[BuildingExposure]] = {
        town_code: [] for town_code in town_rows
    }
    for row in buildings:
        town_code = _business_key(row, "town_code", "id", "ID")
        if town_code not in buildings_by_town:
            raise ValueError("building row references an unknown town code")
        for structure, area_m2 in _building_areas(row):
            if area_m2 < 0:
                raise ValueError("building area must not be negative")
            if area_m2 == 0:
                continue
            era_value = _optional_text(row, "era")
            buildings_by_town[town_code].append(
                BuildingExposure(
                    town_code=town_code,
                    structure=structure,
                    era=era_value,
                    area_m2=area_m2,
                )
            )

    result: list[TownExposure] = []
    for town_code in sorted(town_rows):
        row = town_rows[town_code]
        population = _number(_properties(row), "population_total", "total")
        if population < 0:
            raise ValueError("population must not be negative")
        result.append(
            TownExposure(
                town_code=town_code,
                county_code=_county_code(row, town_code),
                town_name=_required_text(row, "town_name", "NAME", "name"),
                population_total=population,
                geometry_wkt=geometry_rows[town_code],
                buildings=tuple(
                    sorted(
                        buildings_by_town[town_code],
                        key=lambda item: (item.structure.value, item.era or ""),
                    )
                ),
            )
        )
    return ExposureDataset(
        snapshot_checksum=snapshot_checksum,
        city=city_exposure,
        towns=tuple(result),
    )


class LossExposureService:
    def __init__(
        self,
        *,
        snapshots,
        profile: RegionLossProfile | None = None,
        list_records=None,
        list_features=None,
    ) -> None:
        self._profile = profile or load_region_loss_profile(
            settings.loss_region_profile_path
        )
        self._bridge = LossAssetBridge(
            snapshots=snapshots,
            profile=self._profile,
            list_records=list_records,
            list_features=list_features,
        )

    async def prepare(self, session, *, run_id) -> ExposureDataset:
        run = await session.get(AssessmentRun, run_id)
        if run is None:
            raise LookupError("assessment run not found")
        region_id = run.snapshot.get("region_id")
        if not isinstance(region_id, str) or not region_id.strip():
            raise ValueError("assessment run snapshot requires region_id")
        return await self._bridge.lock_and_load(
            session,
            run_id=run_id,
            region_id=region_id,
        )
