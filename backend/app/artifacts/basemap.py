from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

MIN_TILE_ZOOM = 0
MAX_TILE_ZOOM = 22
TILE_SIZE = 256
EARTH_CIRCUMFERENCE_M = 40_075_016.685_578_49
WEB_MERCATOR_MAX_LATITUDE = 85.051_128_779_806_6
DEFAULT_BUFFER_PIXELS = 256
DEFAULT_MAP_OUTPUT_WIDTH = 4761
DEFAULT_MAP_OUTPUT_HEIGHT = 3369
DEFAULT_ZOOM_LEVELS = (9, 10, 11, 12)
VIEWPORT_RADII_KM = (2.5, 5.0, 10.0, 30.0, 50.0)


def _sha256_json(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _datetime_json(value: datetime) -> str:
    return _normalize_utc(value).isoformat()


def _finite_float(value: float, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _normalize_zoom_levels(zoom_levels: Iterable[int]) -> tuple[int, ...]:
    result: list[int] = []
    for value in zoom_levels:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("zoom levels must be integers")
        if not MIN_TILE_ZOOM <= value <= MAX_TILE_ZOOM:
            raise ValueError(
                f"zoom levels must be between {MIN_TILE_ZOOM} and {MAX_TILE_ZOOM}"
            )
        result.append(value)
    if not result:
        raise ValueError("zoom levels must not be empty")
    return tuple(sorted(set(result)))


def _normalize_tiles(tiles: Iterable[TileKey]) -> tuple["TileKey", ...]:
    return tuple(sorted(set(tiles)))


def _mercator_x(lon: float) -> float:
    return (lon + 180.0) / 360.0 * EARTH_CIRCUMFERENCE_M


def _mercator_y(lat: float) -> float:
    latitude = math.radians(lat)
    return (
        (1.0 - math.asinh(math.tan(latitude)) / math.pi)
        / 2.0
        * EARTH_CIRCUMFERENCE_M
    )


def _tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    tile_count = 2**z
    tile_width = EARTH_CIRCUMFERENCE_M / tile_count
    west = -EARTH_CIRCUMFERENCE_M / 2.0 + x * tile_width
    east = west + tile_width
    north = EARTH_CIRCUMFERENCE_M / 2.0 - y * tile_width
    south = north - tile_width
    return west, south, east, north


def _union_bounds(tiles: Iterable["TileKey"]) -> tuple[float, float, float, float]:
    bounds = [_tile_bounds_3857(tile.z, tile.x, tile.y) for tile in tiles]
    if not bounds:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(item[0] for item in bounds),
        min(item[1] for item in bounds),
        max(item[2] for item in bounds),
        max(item[3] for item in bounds),
    )


def _deterministic_sample(
    keys: tuple["TileKey", ...],
    sample_size: int,
) -> tuple["TileKey", ...]:
    if not keys:
        return ()
    size = min(sample_size, len(keys))
    if size == len(keys):
        return keys
    return tuple(
        keys[math.floor(index * (len(keys) - 1) / (size - 1))]
        for index in range(size)
    )


@dataclass(frozen=True, order=True, slots=True)
class TileKey:
    z: int
    x: int
    y: int

    def __post_init__(self) -> None:
        if not MIN_TILE_ZOOM <= self.z <= MAX_TILE_ZOOM:
            raise ValueError(
                f"tile zoom must be between {MIN_TILE_ZOOM} and {MAX_TILE_ZOOM}"
            )
        if self.x < 0 or self.y < 0 or self.x >= 2**self.z or self.y >= 2**self.z:
            raise ValueError("tile x and y must be inside the zoom-level grid")

    def to_tuple(self) -> tuple[int, int, int]:
        return self.z, self.x, self.y


@dataclass(frozen=True, slots=True)
class MapViewportTileManifest:
    center_lon: float
    center_lat: float
    radius_km: float
    output_width: int
    output_height: int
    zoom_levels: tuple[int, ...]
    buffer_pixels: int
    tiles: tuple[TileKey, ...]

    @classmethod
    def build(
        cls,
        center_lon: float,
        center_lat: float,
        radius_km: float,
        output_width: int,
        output_height: int,
        zoom_levels: Iterable[int],
        *,
        padding: int = DEFAULT_BUFFER_PIXELS,
    ) -> "MapViewportTileManifest":
        lon = _finite_float(center_lon, "center_lon")
        lat = _finite_float(center_lat, "center_lat")
        radius = _finite_float(radius_km, "radius_km")
        if not -180.0 <= lon <= 180.0:
            raise ValueError("center_lon must be between -180 and 180")
        if not -WEB_MERCATOR_MAX_LATITUDE <= lat <= WEB_MERCATOR_MAX_LATITUDE:
            raise ValueError(
                "center_lat must be inside the Web Mercator latitude range"
            )
        if radius <= 0:
            raise ValueError("radius_km must be positive")
        if isinstance(output_width, bool) or not isinstance(output_width, int) or output_width <= 0:
            raise ValueError("output_width must be a positive integer")
        if isinstance(output_height, bool) or not isinstance(output_height, int) or output_height <= 0:
            raise ValueError("output_height must be a positive integer")
        if isinstance(padding, bool) or not isinstance(padding, int) or padding < 0:
            raise ValueError("padding must be a non-negative integer")

        normalized_zooms = _normalize_zoom_levels(zoom_levels)
        tiles: set[TileKey] = set()
        for zoom in normalized_zooms:
            tile_count = 2**zoom
            world_size_px = TILE_SIZE * tile_count
            center_x_px = (lon + 180.0) / 360.0 * world_size_px
            latitude = math.radians(lat)
            center_y_px = (
                (1.0 - math.asinh(math.tan(latitude)) / math.pi)
                / 2.0
                * world_size_px
            )
            resolution_m_per_px = EARTH_CIRCUMFERENCE_M / world_size_px
            radius_px = radius * 1000.0 / resolution_m_per_px
            half_width_px = output_width / 2.0 + padding + radius_px
            half_height_px = output_height / 2.0 + padding + radius_px
            min_x_px = center_x_px - half_width_px
            max_x_px = center_x_px + half_width_px
            min_y_px = center_y_px - half_height_px
            max_y_px = center_y_px + half_height_px
            x_min = math.floor(min_x_px / TILE_SIZE)
            x_max = math.floor((max_x_px - 1.0) / TILE_SIZE)
            y_min = math.floor(min_y_px / TILE_SIZE)
            y_max = math.floor((max_y_px - 1.0) / TILE_SIZE)
            for y in range(y_min, y_max + 1):
                for x in range(x_min, x_max + 1):
                    if 0 <= x < tile_count and 0 <= y < tile_count:
                        tiles.add(TileKey(zoom, x, y))

        return cls(
            center_lon=lon,
            center_lat=lat,
            radius_km=radius,
            output_width=output_width,
            output_height=output_height,
            zoom_levels=normalized_zooms,
            buffer_pixels=padding,
            tiles=_normalize_tiles(tiles),
        )

    @property
    def required_count(self) -> int:
        return len(self.tiles)

    @property
    def manifest_checksum(self) -> str:
        return _sha256_json(self.to_serializable())

    def tiles_for_zoom(self, zoom: int) -> tuple[TileKey, ...]:
        return tuple(tile for tile in self.tiles if tile.z == zoom)

    def union_bounds_3857(self) -> tuple[float, float, float, float]:
        return _union_bounds(self.tiles)

    def to_serializable(self) -> dict[str, Any]:
        return {
            "center_lon": self.center_lon,
            "center_lat": self.center_lat,
            "viewport_radius_km": self.radius_km,
            "output_width": self.output_width,
            "output_height": self.output_height,
            "buffer_pixels": self.buffer_pixels,
            "zoom_levels": list(self.zoom_levels),
            "required_tiles": [tile.to_tuple() for tile in self.tiles],
        }

    def static_entries(self) -> tuple[dict[str, Any], ...]:
        entries: list[dict[str, Any]] = []
        for zoom in self.zoom_levels:
            payload = {
                "viewport_radius_km": self.radius_km,
                "zoom_level": zoom,
                "required_tiles": [
                    tile.to_tuple() for tile in self.tiles_for_zoom(zoom)
                ],
                "buffer_pixels": self.buffer_pixels,
            }
            entry = dict(payload)
            entry["manifest_checksum"] = _sha256_json(entry)
            entries.append(entry)
        return tuple(entries)


@dataclass(slots=True)
class OfflineBasemapPackage:
    provider: str
    package_id: str
    version: str
    checksum: str
    generated_at: datetime
    max_age_days: int
    coverage_bounds: tuple[float, float, float, float]
    zoom_levels: tuple[int, ...]
    tiles: tuple[TileKey, ...]
    files: Mapping[TileKey, bytes]
    format: str = "png"
    source_statement: str = "offline"
    file_count: int | None = None
    record_count: int | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider must not be empty")
        if not self.package_id.strip():
            raise ValueError("package_id must not be empty")
        if not self.version.strip():
            raise ValueError("version must not be empty")
        if not isinstance(self.checksum, str) or len(self.checksum) != 64:
            raise ValueError("checksum must be a 64 character SHA-256 hex digest")
        if isinstance(self.max_age_days, bool) or not isinstance(self.max_age_days, int) or self.max_age_days < 0:
            raise ValueError("max_age_days must be a non-negative integer")
        if not self.coverage_bounds or len(self.coverage_bounds) != 4:
            raise ValueError("coverage_bounds must contain west, south, east and north")
        west, south, east, north = (
            float(self.coverage_bounds[0]),
            float(self.coverage_bounds[1]),
            float(self.coverage_bounds[2]),
            float(self.coverage_bounds[3]),
        )
        if not all(math.isfinite(value) for value in (west, south, east, north)):
            raise ValueError("coverage bounds must be finite")
        if west >= east or south >= north:
            raise ValueError("coverage bounds must have west < east and south < north")
        object.__setattr__(self, "generated_at", _normalize_utc(self.generated_at))
        object.__setattr__(
            self,
            "zoom_levels",
            _normalize_zoom_levels(self.zoom_levels),
        )
        object.__setattr__(self, "tiles", _normalize_tiles(self.tiles))
        object.__setattr__(
            self,
            "files",
            dict(self.files),
        )
        if self.file_count is None:
            object.__setattr__(self, "file_count", len(self.files))
        if self.record_count is None:
            object.__setattr__(self, "record_count", len(self.tiles))
        if isinstance(self.file_count, bool) or not isinstance(self.file_count, int):
            raise ValueError("file_count must be an integer")
        if isinstance(self.record_count, bool) or not isinstance(self.record_count, int):
            raise ValueError("record_count must be an integer")

    def computed_manifest_checksum(self) -> str:
        payload = {
            "provider": self.provider,
            "package_id": self.package_id,
            "version": self.version,
            "generated_at": _datetime_json(self.generated_at),
            "max_age_days": self.max_age_days,
            "coverage_bounds": list(self.coverage_bounds),
            "zoom_levels": list(self.zoom_levels),
            "tiles": [tile.to_tuple() for tile in self.tiles],
            "format": self.format,
            "source_statement": self.source_statement,
        }
        return _sha256_json(payload)

    def decode_tile(self, key: TileKey) -> bytes:
        try:
            data = self.files[key]
        except KeyError as error:
            raise KeyError(f"tile is not present in package: {key.to_tuple()}") from error
        try:
            with Image.open(BytesIO(data)) as image:
                image.load()
        except Exception as error:
            raise ValueError(f"tile decode failed for {key.to_tuple()}") from error
        return data

    def with_recomputed_checksum(self) -> "OfflineBasemapPackage":
        return replace(self, checksum=self.computed_manifest_checksum())

    def with_missing_tile(self, key: TileKey) -> "OfflineBasemapPackage":
        tiles = tuple(tile for tile in self.tiles if tile != key)
        files = {tile: data for tile, data in self.files.items() if tile != key}
        changed = replace(
            self,
            provider="gaode",
            package_id="gaode-offline-v1",
            tiles=tiles,
            files=files,
            file_count=len(files),
            record_count=len(tiles),
        )
        return replace(changed, checksum=changed.computed_manifest_checksum())

    def remove_tile(self, key: TileKey) -> "OfflineBasemapPackage":
        self.tiles = tuple(tile for tile in self.tiles if tile != key)
        self.files = {tile: data for tile, data in self.files.items() if tile != key}
        self.file_count = len(self.files)
        self.record_count = len(self.tiles)
        self.checksum = self.computed_manifest_checksum()
        return self

    def valid_copy(self) -> "OfflineBasemapPackage":
        changed = replace(
            self,
            provider="tianditu",
            package_id="tianditu-offline-v1",
            file_count=len(self.files),
            record_count=len(self.tiles),
        )
        return replace(changed, checksum=changed.computed_manifest_checksum())

    def expired(self) -> "OfflineBasemapPackage":
        changed = replace(
            self,
            generated_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        return replace(changed, checksum=changed.computed_manifest_checksum())

    def with_checksum(self, checksum: str) -> "OfflineBasemapPackage":
        return replace(self, checksum=checksum)

    def with_declared_counts(
        self,
        *,
        file_count: int,
        record_count: int,
    ) -> "OfflineBasemapPackage":
        return replace(
            self,
            file_count=file_count,
            record_count=record_count,
        )


def load_offline_basemap_packages(root: str | Path) -> dict[str, OfflineBasemapPackage]:
    packages: dict[str, OfflineBasemapPackage] = {}
    root_path = Path(root)
    for provider in ("gaode", "tianditu"):
        manifest_path = root_path / provider / "manifest.json"
        if not manifest_path.is_file():
            continue
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        zoom_levels = tuple(int(value) for value in payload["zoom_levels"])
        tiles = tuple(TileKey(*item) for item in payload.get("tiles", ()))
        tile_format = str(payload.get("format", "png"))
        files: dict[TileKey, bytes] = {}
        for tile in tiles:
            tile_path = (
                root_path
                / provider
                / "tiles"
                / str(tile.z)
                / str(tile.x)
                / f"{tile.y}.{tile_format}"
            )
            if tile_path.is_file():
                files[tile] = tile_path.read_bytes()
        generated_at = datetime.fromisoformat(
            str(payload["generated_at"]).replace("Z", "+00:00")
        )
        packages[provider] = OfflineBasemapPackage(
            provider=str(payload["provider"]),
            package_id=str(payload["package_id"]),
            version=str(payload["version"]),
            checksum=str(payload["checksum"]),
            generated_at=generated_at,
            max_age_days=int(payload["max_age_days"]),
            coverage_bounds=tuple(float(value) for value in payload["coverage_bounds"]),
            zoom_levels=zoom_levels,
            tiles=tiles,
            files=files,
            format=tile_format,
            source_statement=str(payload.get("source_statement", "offline")),
        )
    return packages


@dataclass(frozen=True, slots=True)
class BasemapValidationResult:
    provider: str
    package_id: str
    valid: bool
    error_category: str | None = None
    error_message: str | None = None
    missing_tiles: tuple[tuple[int, int, int], ...] = ()
    sampled_decoded: int = 0
    required_tiles: tuple[tuple[int, int, int], ...] = ()


class OfflineBasemapValidator:
    def validate_package(
        self,
        package: OfflineBasemapPackage,
        manifests: Iterable[MapViewportTileManifest],
        *,
        observed_at: datetime,
    ) -> BasemapValidationResult:
        required = _normalize_tiles(
            tile for manifest in manifests for tile in manifest.tiles
        )
        required_tuples = tuple(tile.to_tuple() for tile in required)

        if package.computed_manifest_checksum() != package.checksum:
            return BasemapValidationResult(
                provider=package.provider,
                package_id=package.package_id,
                valid=False,
                error_category="manifest_checksum_mismatch",
                error_message="package manifest checksum does not match the published checksum",
                required_tiles=required_tuples,
            )
        if package.file_count != len(package.files) or package.record_count != len(
            package.tiles
        ):
            return BasemapValidationResult(
                provider=package.provider,
                package_id=package.package_id,
                valid=False,
                error_category="file_record_count_mismatch",
                error_message="declared file or record count does not match package content",
                required_tiles=required_tuples,
            )

        required_west, required_south, required_east, required_north = _union_bounds(
            required
        )
        package_west, package_south, package_east, package_north = (
            float(package.coverage_bounds[0]),
            float(package.coverage_bounds[1]),
            float(package.coverage_bounds[2]),
            float(package.coverage_bounds[3]),
        )
        coverage_tolerance = 1e-6
        if (
            required_west < package_west - coverage_tolerance
            or required_south < package_south - coverage_tolerance
            or required_east > package_east + coverage_tolerance
            or required_north > package_north + coverage_tolerance
        ):
            return BasemapValidationResult(
                provider=package.provider,
                package_id=package.package_id,
                valid=False,
                error_category="coverage_boundary_mismatch",
                error_message="required tile bounds are outside the package coverage bounds",
                required_tiles=required_tuples,
            )

        observed = _normalize_utc(observed_at)
        expires_at = package.generated_at + timedelta(days=package.max_age_days)
        if expires_at < observed:
            return BasemapValidationResult(
                provider=package.provider,
                package_id=package.package_id,
                valid=False,
                error_category="package_expired",
                error_message="offline basemap package has exceeded its maximum age",
                required_tiles=required_tuples,
            )

        package_tiles = set(package.tiles)
        missing = tuple(
            tile.to_tuple() for tile in required if tile not in package_tiles
        )
        if missing:
            return BasemapValidationResult(
                provider=package.provider,
                package_id=package.package_id,
                valid=False,
                error_category="required_tile_missing",
                error_message="one or more exact required tiles are missing",
                missing_tiles=missing,
                required_tiles=required_tuples,
            )

        sample = _deterministic_sample(required, 200)
        sampled = 0
        for tile in sample:
            package.decode_tile(tile)
            sampled += 1

        return BasemapValidationResult(
            provider=package.provider,
            package_id=package.package_id,
            valid=True,
            sampled_decoded=sampled,
            required_tiles=required_tuples,
        )


@dataclass(frozen=True, slots=True)
class SelectedBasemap:
    provider: str
    package_id: str
    version: str
    checksum: str
    selection_reason: str

    @property
    def provider_style_key(self) -> str:
        return f"{self.provider}-local-v1"

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "package_id": self.package_id,
            "version": self.version,
            "checksum": self.checksum,
            "selection_reason": self.selection_reason,
            "provider_style_key": self.provider_style_key,
        }


class AllBasemapsUnavailableError(RuntimeError):
    pass


class BasemapSelector:
    def select(
        self,
        gaode: OfflineBasemapPackage,
        tianditu: OfflineBasemapPackage,
        manifests: Iterable[MapViewportTileManifest],
        observed_at: datetime,
    ) -> SelectedBasemap:
        validator = OfflineBasemapValidator()
        manifests_tuple = tuple(manifests)
        gaode_result = validator.validate_package(
            gaode,
            manifests_tuple,
            observed_at=observed_at,
        )
        if gaode_result.valid:
            return SelectedBasemap(
                provider=gaode.provider,
                package_id=gaode.package_id,
                version=gaode.version,
                checksum=gaode.checksum,
                selection_reason="gaode validated",
            )

        tianditu_result = validator.validate_package(
            tianditu,
            manifests_tuple,
            observed_at=observed_at,
        )
        if tianditu_result.valid:
            return SelectedBasemap(
                provider=tianditu.provider,
                package_id=tianditu.package_id,
                version=tianditu.version,
                checksum=tianditu.checksum,
                selection_reason=(
                    f"gaode invalid: {gaode_result.error_category}; "
                    "tianditu validated"
                ),
            )

        raise AllBasemapsUnavailableError(
            "no validated offline basemap is available: "
            f"gaode={gaode_result.error_category}, "
            f"tianditu={tianditu_result.error_category}"
        )
