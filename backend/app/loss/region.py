from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class RegionAssetKeys:
    admin_city: str
    admin_county: str
    admin_town: str
    population_town: str
    building_town: str
    economy_county: str
    loss_parameters: str
    fault: str | None
    gdp_raster: str | None
    dem_raster: str | None


@dataclass(frozen=True, slots=True)
class RegionLossProfile:
    version: str
    region_id: str
    name: str
    administrative_level: str
    area_crs: str
    output_crs: str
    population_field: str
    population_fields_for_audit: tuple[str, ...]
    affected_population_min_intensity: float
    spatial_allocation_rule: str
    grid_resolution_m: int
    minimum_town_coverage_ratio: float
    grid_residual_review_threshold: float
    default_model_versions: dict[str, str]
    asset_keys: RegionAssetKeys

    def to_snapshot(self) -> dict[str, object]:
        return {
            "version": self.version,
            "region_id": self.region_id,
            "name": self.name,
            "administrative_level": self.administrative_level,
            "area_crs": self.area_crs,
            "output_crs": self.output_crs,
            "population_field": self.population_field,
            "population_fields_for_audit": list(
                self.population_fields_for_audit
            ),
            "affected_population_min_intensity": (
                self.affected_population_min_intensity
            ),
            "spatial_allocation_rule": self.spatial_allocation_rule,
            "grid_resolution_m": self.grid_resolution_m,
            "minimum_town_coverage_ratio": self.minimum_town_coverage_ratio,
            "grid_residual_review_threshold": (
                self.grid_residual_review_threshold
            ),
            "assets": asdict(self.asset_keys),
            "default_model_versions": dict(self.default_model_versions),
        }


def load_region_loss_profile(path: str | Path) -> RegionLossProfile:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("loss region profile must be a mapping")
    return load_region_loss_profile_from_mapping(payload)


def load_region_loss_profile_from_mapping(
    payload: Mapping[str, object],
) -> RegionLossProfile:
    if not isinstance(payload, dict):
        raise ValueError("loss region profile must be a mapping")
    assets = payload.get("assets")
    if not isinstance(assets, dict):
        raise ValueError("loss region profile requires assets")
    required_asset_keys = (
        "admin_city",
        "admin_county",
        "admin_town",
        "population_town",
        "building_town",
        "economy_county",
        "loss_parameters",
    )
    optional_asset_keys = ("fault", "gdp_raster", "dem_raster")

    def required_asset_key(key: str) -> str:
        value = assets.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"loss region profile requires assets.{key}")
        return value.strip()

    def optional_asset_key(key: str) -> str | None:
        value = assets.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"loss region profile assets.{key} must be text or null")
        return value.strip()

    asset_keys = RegionAssetKeys(
        **{key: required_asset_key(key) for key in required_asset_keys},
        **{key: optional_asset_key(key) for key in optional_asset_keys},
    )
    return RegionLossProfile(
        version=str(payload["version"]),
        region_id=str(payload["region_id"]),
        name=str(payload["name"]),
        administrative_level=str(payload["administrative_level"]),
        area_crs=str(payload["area_crs"]),
        output_crs=str(payload["output_crs"]),
        population_field=str(payload["population_field"]),
        population_fields_for_audit=tuple(payload.get("population_fields_for_audit", ())),
        affected_population_min_intensity=float(payload["affected_population_min_intensity"]),
        spatial_allocation_rule=str(payload["spatial_allocation_rule"]),
        grid_resolution_m=int(payload["grid_resolution_m"]),
        minimum_town_coverage_ratio=float(payload["minimum_town_coverage_ratio"]),
        grid_residual_review_threshold=float(
            payload["grid_residual_review_threshold"]
        ),
        default_model_versions={
            str(key): str(value)
            for key, value in dict(payload["default_model_versions"]).items()
        },
        asset_keys=asset_keys,
    )
