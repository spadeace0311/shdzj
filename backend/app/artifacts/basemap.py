from __future__ import annotations

import gzip
import hashlib
import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

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
DEFAULT_DECODE_SAMPLE_SIZE = 200
MANIFEST_SCHEMA_VERSION = 1
_PACKAGE_FORMATS = {"directory", "mbtiles", "pmtiles"}


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


def _spatial_grid_group(
    keys: tuple["TileKey", ...],
    count: int,
) -> set["TileKey"]:
    if count >= len(keys):
        return set(keys)
    xs = sorted({tile.x for tile in keys})
    ys = sorted({tile.y for tile in keys})
    rows = max(1, min(count, math.ceil(math.sqrt(count))))
    columns = max(1, math.ceil(count / rows))
    selected: set[TileKey] = set()
    for row_index in range(rows):
        for column_index in range(columns):
            if len(selected) >= count:
                break
            target_x = xs[
                round((column_index + 0.5) / columns * (len(xs) - 1))
            ]
            target_y = ys[
                round((row_index + 0.5) / rows * (len(ys) - 1))
            ]
            selected.add(
                min(
                    keys,
                    key=lambda tile: (
                        (tile.x - target_x) ** 2 + (tile.y - target_y) ** 2
                    ),
                )
            )
        if len(selected) >= count:
            break
    return selected


def _fixed_spatial_grid_sample(
    keys: tuple["TileKey", ...],
    sample_size: int,
) -> tuple["TileKey", ...]:
    if not keys:
        return ()
    if len(keys) <= sample_size:
        return keys

    by_zoom: dict[int, list[TileKey]] = {}
    for tile in keys:
        by_zoom.setdefault(tile.z, []).append(tile)

    selected: set[TileKey] = set()
    for zoom in sorted(by_zoom):
        if len(selected) >= sample_size:
            break
        group = tuple(sorted(by_zoom[zoom]))
        quota = max(
            1,
            min(
                round(sample_size * len(group) / len(keys)),
                sample_size - len(selected),
                len(group),
            ),
        )
        selected.update(_spatial_grid_group(group, quota))

    for tile in keys:
        if len(selected) >= sample_size:
            break
        selected.add(tile)
    return tuple(sorted(selected))


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
        latitude = math.radians(lat)
        tiles: set[TileKey] = set()
        for zoom in normalized_zooms:
            tile_count = 2**zoom
            world_size_px = TILE_SIZE * tile_count
            center_x_px = (lon + 180.0) / 360.0 * world_size_px
            center_y_px = _mercator_y(lat) / EARTH_CIRCUMFERENCE_M * world_size_px
            equatorial_resolution_m_per_px = (
                EARTH_CIRCUMFERENCE_M / world_size_px
            )
            mercator_radius_m = radius * 1000.0 / math.cos(latitude)
            radius_px = mercator_radius_m / equatorial_resolution_m_per_px
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
    def mercator_radius_m(self) -> float:
        return self.radius_km * 1000.0 / math.cos(math.radians(self.center_lat))

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


class OfflineTileStore(Protocol):
    @property
    def tile_count(self) -> int:
        ...

    @property
    def file_count(self) -> int:
        ...

    @property
    def record_count(self) -> int:
        ...

    def tile_keys(self) -> tuple[TileKey, ...]:
        ...

    def read_tile(self, key: TileKey) -> bytes:
        ...


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
    files: Mapping[TileKey, bytes] = field(default_factory=dict)
    format: str = "png"
    source_statement: str = "offline"
    package_format: str = "directory"
    index_file: str | None = None
    tile_count: int | None = None
    file_count: int | None = None
    record_count: int | None = None
    tile_store: OfflineTileStore | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider must not be empty")
        if not self.package_id.strip():
            raise ValueError("package_id must not be empty")
        if not self.version.strip():
            raise ValueError("version must not be empty")
        if not isinstance(self.checksum, str) or len(self.checksum) != 64:
            raise ValueError("checksum must be a 64 character SHA-256 hex digest")
        if self.checksum != self.checksum.lower():
            raise ValueError("checksum must be lowercase")
        try:
            int(self.checksum, 16)
        except ValueError as error:
            raise ValueError("checksum must be a SHA-256 hex digest") from error
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
        if self.package_format not in _PACKAGE_FORMATS:
            raise ValueError(f"unsupported package format: {self.package_format}")
        if self.package_format != "directory" and not self.index_file:
            raise ValueError("mbtiles and pmtiles packages require an index_file")
        if self.format != self.format.lower():
            raise ValueError("tile format must be lowercase")

        object.__setattr__(self, "generated_at", _normalize_utc(self.generated_at))
        object.__setattr__(
            self,
            "coverage_bounds",
            (west, south, east, north),
        )
        object.__setattr__(
            self,
            "zoom_levels",
            _normalize_zoom_levels(self.zoom_levels),
        )
        object.__setattr__(self, "tiles", _normalize_tiles(self.tiles))
        object.__setattr__(self, "files", dict(self.files))

        if self.tile_count is None:
            object.__setattr__(self, "tile_count", len(self.tiles))
        if self.file_count is None:
            object.__setattr__(
                self,
                "file_count",
                self.tile_store.file_count
                if self.tile_store is not None
                else len(self.files),
            )
        if self.record_count is None:
            object.__setattr__(
                self,
                "record_count",
                self.tile_store.record_count
                if self.tile_store is not None
                else len(self.tiles),
            )
        for label, value in (
            ("tile_count", self.tile_count),
            ("file_count", self.file_count),
            ("record_count", self.record_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")

    @property
    def actual_tile_count(self) -> int:
        return len(self.tiles)

    @property
    def actual_file_count(self) -> int:
        if self.tile_store is not None:
            return self.tile_store.file_count
        return len(self.files)

    @property
    def actual_record_count(self) -> int:
        if self.tile_store is not None:
            return self.tile_store.record_count
        return len(self.tiles)

    def _manifest_payload(self) -> dict[str, Any]:
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "provider": self.provider,
            "package_id": self.package_id,
            "version": self.version,
            "generated_at": _datetime_json(self.generated_at),
            "max_age_days": self.max_age_days,
            "coverage_bounds": list(self.coverage_bounds),
            "zoom_levels": list(self.zoom_levels),
            "tile_format": self.format,
            "source_statement": self.source_statement,
            "package_format": self.package_format,
            "index_file": self.index_file,
            "tile_count": self.tile_count,
            "file_count": self.file_count,
            "record_count": self.record_count,
        }

    def computed_manifest_checksum(self) -> str:
        return _sha256_json(self._manifest_payload())

    def to_manifest_entry(self) -> dict[str, Any]:
        payload = self._manifest_payload()
        payload["checksum"] = self.checksum
        payload["format"] = self.format
        payload["coverage"] = list(self.coverage_bounds)
        return payload

    def decode_tile(self, key: TileKey) -> bytes:
        if key not in self.tiles:
            raise ValueError(f"tile coordinate is not in package index: {key.to_tuple()}")
        try:
            if self.tile_store is not None:
                data = self.tile_store.read_tile(key)
            else:
                data = self.files[key]
        except KeyError as error:
            raise ValueError(f"tile is not present in package: {key.to_tuple()}") from error
        self._verify_tile_image(key, data)
        return data

    def _verify_tile_image(self, key: TileKey, data: bytes) -> None:
        try:
            with Image.open(BytesIO(data)) as image:
                image.load()
                if image.size != (TILE_SIZE, TILE_SIZE):
                    raise ValueError(
                        f"tile image must be {TILE_SIZE}x{TILE_SIZE}"
                    )
                if image.getbbox() is None:
                    raise ValueError("tile image must contain non-empty pixels")
        except ValueError:
            raise
        except Exception as error:
            raise ValueError(f"tile decode failed for {key.to_tuple()}") from error

    def with_recomputed_checksum(self) -> "OfflineBasemapPackage":
        return replace(self, checksum=self.computed_manifest_checksum())

    def with_missing_tile(self, key: TileKey) -> "OfflineBasemapPackage":
        tiles = tuple(tile for tile in self.tiles if tile != key)
        files = {tile: data for tile, data in self.files.items() if tile != key}
        changed = replace(
            self,
            tiles=tiles,
            files=files,
            tile_count=len(tiles),
            file_count=len(files),
            record_count=len(tiles),
        )
        return replace(changed, checksum=changed.computed_manifest_checksum())

    def remove_tile(self, key: TileKey) -> "OfflineBasemapPackage":
        self.tiles = tuple(tile for tile in self.tiles if tile != key)
        self.files = {tile: data for tile, data in self.files.items() if tile != key}
        self.tile_count = len(self.tiles)
        self.file_count = len(self.files)
        self.record_count = len(self.tiles)
        self.checksum = self.computed_manifest_checksum()
        return self

    def valid_copy(self) -> "OfflineBasemapPackage":
        changed = replace(
            self,
            provider="tianditu",
            package_id="tianditu-offline-v1",
            tile_count=len(self.tiles),
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
        tile_count: int | None = None,
        file_count: int | None = None,
        record_count: int | None = None,
    ) -> "OfflineBasemapPackage":
        changed = replace(
            self,
            tile_count=self.tile_count if tile_count is None else tile_count,
            file_count=self.file_count if file_count is None else file_count,
            record_count=self.record_count if record_count is None else record_count,
        )
        return replace(changed, checksum=changed.computed_manifest_checksum())


def _read_varint(data: bytes, position: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while position < len(data):
        current = data[position]
        position += 1
        result |= (current & 0x7F) << shift
        shift += 7
        if not current & 0x80:
            return result, position
        if shift > 70:
            raise ValueError("PMTiles varint exceeds 64-bit limit")
    raise ValueError("PMTiles directory ended inside a varint")


def _pmtiles_zxy_to_tileid(z: int, x: int, y: int) -> int:
    acc = ((1 << (z * 2)) - 1) // 3
    level = z - 1
    while level >= 0:
        size = 1 << level
        rx = size & x
        ry = size & y
        acc += ((3 * rx) ^ ry) << level
        if ry == 0:
            if rx:
                x = size - 1 - x
                y = size - 1 - y
            x, y = y, x
        level -= 1
    return acc


def _pmtiles_tileid_to_zxy(tile_id: int) -> tuple[int, int, int]:
    z = ((3 * tile_id + 1).bit_length() - 1) // 2
    acc = ((1 << (z * 2)) - 1) // 3
    position = tile_id - acc
    x = 0
    y = 0
    size = 1
    limit = 1 << z
    while size < limit:
        rx = (position // 2) & size
        ry = (position ^ rx) & size
        if ry == 0:
            if rx:
                x = size - 1 - x
                y = size - 1 - y
            x, y = y, x
        x += rx
        y += ry
        position >>= 1
        size <<= 1
    return z, x, y


class _MBTilesTileStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        connection = sqlite3.connect(str(path))
        try:
            rows = connection.execute(
                "SELECT zoom_level, tile_column, tile_row FROM tiles "
                "ORDER BY zoom_level, tile_column, tile_row"
            ).fetchall()
            self._connection = connection
        except Exception:
            connection.close()
            raise
        self._tiles = tuple(TileKey(*row) for row in rows)

    @property
    def tile_count(self) -> int:
        return len(self._tiles)

    @property
    def file_count(self) -> int:
        return 1

    @property
    def record_count(self) -> int:
        return len(self._tiles)

    def tile_keys(self) -> tuple[TileKey, ...]:
        return self._tiles

    def read_tile(self, key: TileKey) -> bytes:
        row = self._connection.execute(
            "SELECT tile_data FROM tiles "
            "WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
            (key.z, key.x, key.y),
        ).fetchone()
        if row is None:
            raise KeyError(key.to_tuple())
        return row[0]


class _PMTilesTileStore:
    def __init__(self, path: Path) -> None:
        self._data = path.read_bytes()
        self._header = self._parse_header()
        self._entries: dict[int, tuple[int, int]] = {}
        self._parse_directory(
            self._data[
                self._header["root_offset"] :
                self._header["root_offset"] + self._header["root_length"]
            ],
            depth=0,
        )
        self._tiles = tuple(
            sorted(
                TileKey(*_pmtiles_tileid_to_zxy(tile_id))
                for tile_id in self._entries
            )
        )

    @property
    def tile_count(self) -> int:
        return len(self._tiles)

    @property
    def file_count(self) -> int:
        return 1

    @property
    def record_count(self) -> int:
        return len(self._tiles)

    def tile_keys(self) -> tuple[TileKey, ...]:
        return self._tiles

    def _parse_header(self) -> dict[str, Any]:
        data = self._data
        if len(data) < 127 or data[:7] != b"PMTiles" or data[7] != 3:
            raise ValueError("unsupported or truncated PMTiles package")

        def uint64(position: int) -> int:
            return int.from_bytes(data[position : position + 8], "little")

        return {
            "root_offset": uint64(8),
            "root_length": uint64(16),
            "metadata_offset": uint64(24),
            "metadata_length": uint64(32),
            "leaf_directory_offset": uint64(40),
            "leaf_directory_length": uint64(48),
            "tile_data_offset": uint64(56),
            "tile_data_length": uint64(64),
            "addressed_tiles_count": uint64(72),
            "tile_entries_count": uint64(80),
            "tile_contents_count": uint64(88),
            "clustered": data[96] == 1,
            "internal_compression": data[97],
            "tile_compression": data[98],
            "tile_type": data[99],
            "min_zoom": data[100],
            "max_zoom": data[101],
        }

    def _decompress_internal(self, data: bytes) -> bytes:
        compression = self._header["internal_compression"]
        if compression in {0, 1}:
            return data
        if compression == 2:
            return gzip.decompress(data)
        raise ValueError(
            f"unsupported PMTiles internal compression: {compression}"
        )

    def _decompress_tile(self, data: bytes) -> bytes:
        compression = self._header["tile_compression"]
        if compression in {0, 1}:
            return data
        if compression == 2:
            return gzip.decompress(data)
        raise ValueError(f"unsupported PMTiles tile compression: {compression}")

    def _parse_directory(self, raw: bytes, *, depth: int) -> None:
        if depth > 1:
            raise ValueError("PMTiles leaf directories exceed the supported depth")
        data = self._decompress_internal(raw)
        position = 0
        count, position = _read_varint(data, position)
        if count <= 0:
            raise ValueError("PMTiles directory must not be empty")

        tile_ids = [0] * count
        previous = 0
        for index in range(count):
            delta, position = _read_varint(data, position)
            previous += delta
            tile_ids[index] = previous

        run_lengths = [0] * count
        for index in range(count):
            run_lengths[index], position = _read_varint(data, position)

        lengths = [0] * count
        for index in range(count):
            length, position = _read_varint(data, position)
            if length <= 0:
                raise ValueError("PMTiles entry length must be positive")
            lengths[index] = length

        offsets = [0] * count
        previous_offset = 0
        previous_length = 0
        for index in range(count):
            encoded, position = _read_varint(data, position)
            if index > 0 and encoded == 0:
                offsets[index] = previous_offset + previous_length
            else:
                offsets[index] = encoded - 1
            previous_offset = offsets[index]
            previous_length = lengths[index]

        leaf_offset = self._header["leaf_directory_offset"]
        leaf_length = self._header["leaf_directory_length"]
        for tile_id, run_length, length, offset in zip(
            tile_ids,
            run_lengths,
            lengths,
            offsets,
            strict=True,
        ):
            if run_length == 0:
                start = leaf_offset + offset
                end = start + length
                if start < leaf_offset or end > leaf_offset + leaf_length:
                    raise ValueError("PMTiles leaf directory points outside archive")
                self._parse_directory(
                    self._data[start:end],
                    depth=depth + 1,
                )
                continue
            for step in range(run_length):
                self._entries[tile_id + step] = (offset, length)

    def read_tile(self, key: TileKey) -> bytes:
        tile_id = _pmtiles_zxy_to_tileid(key.z, key.x, key.y)
        try:
            offset, length = self._entries[tile_id]
        except KeyError as error:
            raise KeyError(key.to_tuple()) from error
        start = self._header["tile_data_offset"] + offset
        return self._decompress_tile(self._data[start : start + length])


def _parse_manifest_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("generated_at must be a non-empty string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _manifest_int(value: object, label: str, *, required: bool = True) -> int:
    if value is None and not required:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return int(value)


def _manifest_float(value: object, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _validate_package_manifest(payload: Mapping[str, Any], provider: str) -> dict[str, Any]:
    schema_version = payload.get("schema_version", MANIFEST_SCHEMA_VERSION)
    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported basemap manifest schema version: {schema_version}"
        )
    declared_provider = payload.get("provider")
    if declared_provider != provider:
        raise ValueError(
            f"basemap provider identity mismatch: manifest={declared_provider}, "
            f"directory={provider}"
        )
    package_format = str(payload.get("package_format", "directory"))
    if package_format not in _PACKAGE_FORMATS:
        raise ValueError(f"unsupported package format: {package_format}")

    package_id = payload.get("package_id")
    version = payload.get("version")
    checksum = payload.get("checksum")
    generated_at = payload.get("generated_at")
    max_age_days = payload.get("max_age_days")
    coverage_bounds = payload.get("coverage_bounds")
    zoom_levels = payload.get("zoom_levels")
    source_statement = payload.get("source_statement", "offline")
    if not isinstance(package_id, str) or not package_id:
        raise ValueError("package_id must be a non-empty string")
    if not isinstance(version, str) or not version:
        raise ValueError("version must be a non-empty string")
    if not isinstance(checksum, str) or len(checksum) != 64:
        raise ValueError("checksum must be a 64 character SHA-256 hex digest")
    if not isinstance(source_statement, str):
        raise ValueError("source_statement must be a string")
    if not isinstance(zoom_levels, list) or not zoom_levels:
        raise ValueError("zoom_levels must be a non-empty list")
    if not isinstance(coverage_bounds, list) or len(coverage_bounds) != 4:
        raise ValueError("coverage_bounds must contain west, south, east and north")
    if not isinstance(generated_at, str):
        raise ValueError("generated_at must be a string")

    tile_format = str(
        payload.get("tile_format", payload.get("format", "png"))
    ).lower()
    index_file = payload.get("index_file")
    if package_format != "directory":
        if not isinstance(index_file, str) or not index_file:
            raise ValueError("mbtiles and pmtiles packages require index_file")
        expected_suffix = f".{package_format}"
        if not index_file.lower().endswith(expected_suffix):
            raise ValueError(
                f"index_file must use the {expected_suffix} suffix"
            )

    return {
        "provider": provider,
        "package_id": package_id,
        "version": version,
        "checksum": checksum.lower(),
        "generated_at": _parse_manifest_datetime(generated_at),
        "max_age_days": _manifest_int(max_age_days, "max_age_days"),
        "coverage_bounds": tuple(
            _manifest_float(value, f"coverage_bounds[{index}]")
            for index, value in enumerate(coverage_bounds)
        ),
        "zoom_levels": tuple(
            _manifest_int(value, f"zoom_levels[{index}]")
            for index, value in enumerate(zoom_levels)
        ),
        "tile_format": tile_format,
        "source_statement": source_statement,
        "package_format": package_format,
        "index_file": index_file if package_format != "directory" else None,
        "tile_count": _manifest_int(
            payload.get("tile_count"),
            "tile_count",
            required=package_format != "directory",
        ),
        "file_count": _manifest_int(
            payload.get("file_count"),
            "file_count",
            required=package_format != "directory",
        ),
        "record_count": _manifest_int(
            payload.get("record_count"),
            "record_count",
            required=package_format != "directory",
        ),
        "tiles": tuple(
            TileKey(*tuple(item))
            for item in payload.get("tiles", ())
        ),
    }


def load_offline_basemap_packages(root: str | Path) -> dict[str, OfflineBasemapPackage]:
    packages: dict[str, OfflineBasemapPackage] = {}
    root_path = Path(root)
    for provider in ("gaode", "tianditu"):
        provider_dir = root_path / provider
        manifest_path = provider_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError(f"{manifest_path} must contain a JSON object")
        meta = _validate_package_manifest(payload, provider)

        tile_store: OfflineTileStore | None = None
        files: dict[TileKey, bytes] = {}
        if meta["package_format"] == "directory":
            tiles = meta["tiles"]
            for tile in tiles:
                tile_path = (
                    provider_dir
                    / "tiles"
                    / str(tile.z)
                    / str(tile.x)
                    / f"{tile.y}.{meta['tile_format']}"
                )
                if not tile_path.is_file():
                    raise FileNotFoundError(
                        f"offline basemap tile file is missing: {tile_path}"
                    )
                files[tile] = tile_path.read_bytes()
        elif meta["package_format"] == "mbtiles":
            index_path = provider_dir / meta["index_file"]
            if not index_path.is_file():
                raise FileNotFoundError(
                    f"offline basemap MBTiles file is missing: {index_path}"
                )
            tile_store = _MBTilesTileStore(index_path)
        elif meta["package_format"] == "pmtiles":
            index_path = provider_dir / meta["index_file"]
            if not index_path.is_file():
                raise FileNotFoundError(
                    f"offline basemap PMTiles file is missing: {index_path}"
                )
            tile_store = _PMTilesTileStore(index_path)
        else:
            raise ValueError(f"unsupported package format: {meta['package_format']}")

        tiles = tile_store.tile_keys() if tile_store is not None else meta["tiles"]
        package = OfflineBasemapPackage(
            provider=meta["provider"],
            package_id=meta["package_id"],
            version=meta["version"],
            checksum=meta["checksum"],
            generated_at=meta["generated_at"],
            max_age_days=meta["max_age_days"],
            coverage_bounds=meta["coverage_bounds"],
            zoom_levels=meta["zoom_levels"],
            tiles=tiles,
            files=files,
            format=meta["tile_format"],
            source_statement=meta["source_statement"],
            package_format=meta["package_format"],
            index_file=meta["index_file"],
            tile_count=meta["tile_count"] or len(tiles),
            file_count=meta["file_count"] or tile_store.file_count
            if tile_store is not None
            else len(files),
            record_count=meta["record_count"] or tile_store.record_count
            if tile_store is not None
            else len(tiles),
            tile_store=tile_store,
        )
        packages[provider] = package
    return packages


@dataclass(frozen=True, slots=True)
class BasemapValidationResult:
    provider: str
    package_id: str
    valid: bool
    error_category: str | None = None
    error_message: str | None = None
    missing_tiles: tuple[tuple[int, int, int], ...] = ()
    tile_coordinates: tuple[int, int, int] | None = None
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

        if (
            package.tile_count != package.actual_tile_count
            or package.file_count != package.actual_file_count
            or package.record_count != package.actual_record_count
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

        sample = _fixed_spatial_grid_sample(required, DEFAULT_DECODE_SAMPLE_SIZE)
        sampled = 0
        for tile in sample:
            try:
                package.decode_tile(tile)
            except Exception as error:
                return BasemapValidationResult(
                    provider=package.provider,
                    package_id=package.package_id,
                    valid=False,
                    error_category="tile_decode_failed",
                    error_message=str(error),
                    tile_coordinates=tile.to_tuple(),
                    sampled_decoded=sampled,
                    required_tiles=required_tuples,
                )
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
    def __init__(
        self,
        message: str,
        *,
        gaode_error: str | None = None,
        tianditu_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.gaode_error = gaode_error
        self.tianditu_error = tianditu_error


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
        tianditu_result = validator.validate_package(
            tianditu,
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
            f"tianditu={tianditu_result.error_category}",
            gaode_error=gaode_result.error_category,
            tianditu_error=tianditu_result.error_category,
        )
