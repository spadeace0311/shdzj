from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pyproj import CRS, Transformer
from shapely import wkt as shapely_wkt
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

from app.data_assets.domain import (
    NormalizedAssetData,
    NormalizedRasterData,
    NormalizedTableData,
    ValidationIssue,
)
from app.intensity.region import RegionProfile


@dataclass(frozen=True, slots=True)
class CoverageRule:
    asset_key: str
    mode: str
    basis: str

    def __post_init__(self) -> None:
        if not self.asset_key.strip():
            raise ValueError("coverage rule asset_key must not be empty")
        if self.mode not in {"full", "partial"}:
            raise ValueError("coverage rule mode must be full or partial")
        if self.basis not in {"asset", "region"}:
            raise ValueError("coverage rule basis must be asset or region")
        if self.mode == "partial" and self.basis != "asset":
            raise ValueError("partial coverage rules must use asset basis")


@dataclass(frozen=True, slots=True)
class RegionCoveragePolicy:
    version: str
    region_id: str
    region_profile_path: str
    warning_ratio: float
    error_ratio: float
    coverage_rules: tuple[CoverageRule, ...]

    def __post_init__(self) -> None:
        required_text = (
            self.version,
            self.region_id,
            self.region_profile_path,
        )
        if any(not value.strip() for value in required_text):
            raise ValueError("coverage policy text fields must not be empty")
        if not 0 <= self.error_ratio <= self.warning_ratio <= 1:
            raise ValueError(
                "coverage policy ratios must satisfy 0 <= error <= warning <= 1"
            )
        asset_keys = [rule.asset_key for rule in self.coverage_rules]
        if len(asset_keys) != len(set(asset_keys)):
            raise ValueError("coverage policy asset rules must not overlap")

    def rule_for(self, asset_key: str) -> CoverageRule | None:
        for rule in self.coverage_rules:
            if rule.asset_key == asset_key:
                return rule
        return None


@dataclass(frozen=True, slots=True)
class RegionCoverageResult:
    statistics: dict[str, object]
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()


def load_region_coverage_policy(path: str | Path) -> RegionCoveragePolicy:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("coverage policy must be a mapping")
    coverage_rules = payload.get("coverage_rules", {})
    if not isinstance(coverage_rules, dict):
        raise ValueError("coverage policy coverage_rules must be a mapping")
    return RegionCoveragePolicy(
        version=str(payload["version"]),
        region_id=str(payload["region_id"]),
        region_profile_path=str(payload["region_profile_path"]),
        warning_ratio=float(payload["warning_ratio"]),
        error_ratio=float(payload["error_ratio"]),
        coverage_rules=tuple(
            CoverageRule(
                asset_key=str(asset_key),
                mode=str(rule["mode"]),
                basis=str(rule["basis"]),
            )
            for asset_key, rule in coverage_rules.items()
            if isinstance(rule, dict)
        ),
    )


def load_policy_region_profile(
    policy: RegionCoveragePolicy,
) -> RegionProfile:
    from app.intensity.region import load_region_profile

    return load_region_profile(policy.region_profile_path)


def evaluate_region_coverage(
    *,
    policy: RegionCoveragePolicy,
    profile: RegionProfile,
    asset_key: str,
    normalized: NormalizedAssetData,
    boundary_wkt: str | None,
) -> RegionCoverageResult:
    statistics: dict[str, object] = {
        "area_crs": profile.grid_crs,
        "projected_area": None,
        "coverage_ratio": None,
        "coverage_status": "not_applicable",
        "coverage_policy_version": policy.version,
    }
    if policy.region_id != profile.region_id:
        return _configuration_error(
            statistics,
            "coverage policy and region profile use different region_id values",
        )

    asset_geometry = _asset_geometry(normalized)
    if asset_geometry is None:
        return RegionCoverageResult(statistics=statistics)

    rule = policy.rule_for(asset_key)
    if rule is None:
        statistics["coverage_status"] = "policy_missing"
        return RegionCoverageResult(
            statistics=statistics,
            errors=(
                ValidationIssue(
                    severity="error",
                    code="coverage_policy_missing",
                    message=(
                        f"spatial asset {asset_key} has no configured "
                        "coverage mode"
                    ),
                ),
            ),
        )
    if boundary_wkt is None:
        statistics["coverage_status"] = "boundary_missing"
        return RegionCoverageResult(
            statistics=statistics,
            errors=(
                ValidationIssue(
                    severity="error",
                    code="region_boundary_missing",
                    message="an active region boundary is required",
                ),
            ),
        )

    try:
        target_crs = CRS.from_user_input(profile.grid_crs)
        transformer = Transformer.from_crs(
            4326,
            target_crs,
            always_xy=True,
        )
        projected_asset = transform(transformer.transform, asset_geometry)
        projected_boundary = transform(
            transformer.transform,
            shapely_wkt.loads(boundary_wkt),
        )
    except Exception as exc:
        return _configuration_error(
            statistics,
            f"coverage CRS transformation failed: {exc}",
        )

    statistics["projected_area"] = float(abs(projected_asset.area))
    if rule.mode == "partial":
        statistics["coverage_ratio"] = _partial_coverage_ratio(
            projected_asset,
            projected_boundary,
        )
        statistics["coverage_status"] = "partial"
        return RegionCoverageResult(statistics=statistics)

    denominator = (
        abs(projected_boundary.area)
        if rule.basis == "region"
        else abs(projected_asset.area)
    )
    coverage_ratio = (
        abs(projected_asset.intersection(projected_boundary).area) / denominator
        if denominator > 0
        else 0.0
    )
    coverage_ratio = min(max(coverage_ratio, 0.0), 1.0)
    statistics["coverage_ratio"] = coverage_ratio
    if coverage_ratio < policy.error_ratio:
        statistics["coverage_status"] = "insufficient"
        return RegionCoverageResult(
            statistics=statistics,
            errors=(
                ValidationIssue(
                    severity="error",
                    code="region_coverage_insufficient",
                    message=(
                        f"spatial asset coverage {coverage_ratio:.6f} is below "
                        f"the required {policy.error_ratio:.6f}"
                    ),
                ),
            ),
        )
    if coverage_ratio < policy.warning_ratio:
        statistics["coverage_status"] = "warning"
        return RegionCoverageResult(
            statistics=statistics,
            warnings=(
                ValidationIssue(
                    severity="warning",
                    code="region_coverage_warning",
                    message=(
                        f"spatial asset coverage {coverage_ratio:.6f} is below "
                        f"the warning threshold {policy.warning_ratio:.6f}"
                    ),
                ),
            ),
        )
    statistics["coverage_status"] = "sufficient"
    return RegionCoverageResult(statistics=statistics)


def _asset_geometry(normalized: NormalizedAssetData) -> BaseGeometry | None:
    if isinstance(normalized, NormalizedRasterData):
        return box(*normalized.spatial_extent)
    if not isinstance(normalized, NormalizedTableData):
        return None
    geometries = [
        shapely_wkt.loads(record.geometry_wkt)
        for record in normalized.records
        if record.geometry_wkt is not None
    ]
    if not geometries:
        return None
    return unary_union(geometries)


def _partial_coverage_ratio(
    asset_geometry: BaseGeometry,
    boundary_geometry: BaseGeometry,
) -> float:
    if asset_geometry.is_empty:
        return 0.0
    if asset_geometry.geom_type in {"LineString", "MultiLineString"}:
        denominator = asset_geometry.length
        numerator = asset_geometry.intersection(boundary_geometry).length
    elif asset_geometry.geom_type in {"Point", "MultiPoint"}:
        covered = sum(
            boundary_geometry.covers(point)
            for point in (
                asset_geometry.geoms
                if hasattr(asset_geometry, "geoms")
                else (asset_geometry,)
            )
        )
        return covered / max(1, len(asset_geometry.geoms)) if hasattr(
            asset_geometry, "geoms"
        ) else float(covered)
    else:
        denominator = asset_geometry.area
        numerator = asset_geometry.intersection(boundary_geometry).area
    if denominator <= 0:
        return 0.0
    return min(max(abs(numerator) / abs(denominator), 0.0), 1.0)


def _configuration_error(
    statistics: dict[str, object],
    message: str,
) -> RegionCoverageResult:
    statistics["coverage_status"] = "configuration_error"
    return RegionCoverageResult(
        statistics=statistics,
        errors=(
            ValidationIssue(
                severity="error",
                code="coverage_configuration_invalid",
                message=message,
            ),
        ),
    )


def geometry_has_area(geometry: BaseGeometry) -> bool:
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return True
    if isinstance(geometry, GeometryCollection):
        return any(geometry_has_area(part) for part in geometry.geoms)
    return False
