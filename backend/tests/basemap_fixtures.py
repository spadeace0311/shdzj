from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from app.artifacts.basemap import (
    EARTH_CIRCUMFERENCE_M,
    MapViewportTileManifest,
    OfflineBasemapPackage,
    TileKey,
)


def png_tile_bytes(
    *,
    color: tuple[int, int, int] = (245, 246, 248),
    size: tuple[int, int] = (256, 256),
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def _tile_bounds(key: TileKey) -> tuple[float, float, float, float]:
    tile_width = EARTH_CIRCUMFERENCE_M / (2**key.z)
    west = -EARTH_CIRCUMFERENCE_M / 2.0 + key.x * tile_width
    east = west + tile_width
    north = EARTH_CIRCUMFERENCE_M / 2.0 - key.y * tile_width
    south = north - tile_width
    return west, south, east, north


def union_bounds_3857(tiles: tuple[TileKey, ...]) -> tuple[float, float, float, float]:
    if not tiles:
        return (0.0, 0.0, 0.0, 0.0)
    bounds = [_tile_bounds(tile) for tile in tiles]
    return (
        min(item[0] for item in bounds),
        min(item[1] for item in bounds),
        max(item[2] for item in bounds),
        max(item[3] for item in bounds),
    )


def manifest_checksum(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def manifest_payload(
    *,
    provider: str,
    package_id: str,
    version: str,
    generated_at: datetime,
    max_age_days: int,
    coverage_bounds: tuple[float, float, float, float],
    zoom_levels: tuple[int, ...],
    tile_format: str,
    source_statement: str,
    package_format: str,
    index_file: str | None,
    tile_count: int,
    file_count: int,
    record_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "provider": provider,
        "package_id": package_id,
        "version": version,
        "generated_at": generated_at.astimezone(UTC).isoformat(),
        "max_age_days": max_age_days,
        "coverage_bounds": list(coverage_bounds),
        "zoom_levels": list(zoom_levels),
        "tile_format": tile_format,
        "source_statement": source_statement,
        "package_format": package_format,
        "index_file": index_file,
        "tile_count": tile_count,
        "file_count": file_count,
        "record_count": record_count,
    }


def make_in_memory_package(
    manifests: tuple[MapViewportTileManifest, ...] | list[MapViewportTileManifest],
    *,
    provider: str,
    generated_at: datetime | None = None,
    tile_bytes: bytes | None = None,
    package_id: str | None = None,
    version: str = "v1",
) -> OfflineBasemapPackage:
    tiles = tuple(
        sorted(
            {
                tile
                for manifest in manifests
                for tile in manifest.tiles
            }
        )
    )
    zoom_levels = tuple(sorted({tile.z for tile in tiles}))
    files = {tile: tile_bytes or png_tile_bytes() for tile in tiles}
    package = OfflineBasemapPackage(
        provider=provider,
        package_id=package_id or f"{provider}-offline-v1",
        version=version,
        checksum="0" * 64,
        generated_at=generated_at or datetime.now(UTC),
        max_age_days=30,
        coverage_bounds=union_bounds_3857(tiles),
        zoom_levels=zoom_levels,
        tiles=tiles,
        files=files,
        format="png",
        source_statement=f"{provider} offline basemap fixture",
        package_format="directory",
        tile_count=len(tiles),
        file_count=len(files),
        record_count=len(tiles),
    )
    return package.with_recomputed_checksum()


def write_mbtiles(
    path: Path,
    tiles: tuple[TileKey, ...],
    tile_bytes: bytes,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "CREATE TABLE tiles ("
            "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, "
            "tile_data BLOB, PRIMARY KEY (zoom_level, tile_column, tile_row)"
            ")"
        )
        connection.executemany(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
            "VALUES (?, ?, ?, ?)",
            [
                (tile.z, tile.x, tile.y, tile_bytes)
                for tile in sorted(tiles)
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _varint(value: int) -> bytes:
    output = bytearray()
    while True:
        part = value & 0x7F
        value >>= 7
        if value:
            output.append(part | 0x80)
        else:
            output.append(part)
            break
    return bytes(output)


def _zxy_to_tileid(z: int, x: int, y: int) -> int:
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


def _write_pmtiles(path: Path, tiles: tuple[TileKey, ...], tile_bytes: bytes) -> None:
    ordered = sorted(tiles, key=lambda tile: _zxy_to_tileid(tile.z, tile.x, tile.y))
    tile_data = bytearray()
    directory_entries: list[tuple[int, int, int]] = []
    for tile in ordered:
        offset = len(tile_data)
        tile_data.extend(tile_bytes)
        directory_entries.append(
            (
                _zxy_to_tileid(tile.z, tile.x, tile.y),
                offset,
                len(tile_bytes),
            )
        )

    directory = bytearray()
    directory.extend(_varint(len(directory_entries)))
    previous_tile_id = 0
    for tile_id, _, _ in directory_entries:
        directory.extend(_varint(tile_id - previous_tile_id))
        previous_tile_id = tile_id
    for _, _, _ in directory_entries:
        directory.extend(_varint(1))
    for _, _, length in directory_entries:
        directory.extend(_varint(length))
    previous_offset = 0
    previous_length = 0
    for _, offset, length in directory_entries:
        if previous_length and offset == previous_offset + previous_length:
            directory.extend(_varint(0))
        else:
            directory.extend(_varint(offset + 1))
        previous_offset = offset
        previous_length = length

    metadata = b'{"provider":"offline-basemap-fixture"}'
    root_offset = 127
    root_length = len(directory)
    metadata_offset = root_offset + root_length
    metadata_length = len(metadata)
    leaf_offset = metadata_offset + metadata_length
    tile_data_offset = leaf_offset

    header = bytearray()
    header.extend(b"PMTiles")
    header.append(3)
    header.extend(struct.pack("<Q", root_offset))
    header.extend(struct.pack("<Q", root_length))
    header.extend(struct.pack("<Q", metadata_offset))
    header.extend(struct.pack("<Q", metadata_length))
    header.extend(struct.pack("<Q", leaf_offset))
    header.extend(struct.pack("<Q", 0))
    header.extend(struct.pack("<Q", tile_data_offset))
    header.extend(struct.pack("<Q", len(tile_data)))
    header.extend(struct.pack("<Q", len(ordered)))
    header.extend(struct.pack("<Q", len(ordered)))
    header.extend(struct.pack("<Q", len(ordered)))
    header.append(0)
    header.append(1)
    header.append(1)
    header.append(2)
    header.append(min(tile.z for tile in ordered))
    header.append(max(tile.z for tile in ordered))
    header.extend(struct.pack("<i", 0))
    header.extend(struct.pack("<i", 0))
    header.extend(struct.pack("<i", 0))
    header.extend(struct.pack("<i", 0))
    header.append(min(tile.z for tile in ordered))
    header.extend(struct.pack("<i", 0))
    header.extend(struct.pack("<i", 0))

    path.write_bytes(bytes(header) + bytes(directory) + metadata + bytes(tile_data))


def write_basemap_package(
    root: Path,
    *,
    provider: str,
    package_format: str,
    tiles: tuple[TileKey, ...],
    zoom_levels: tuple[int, ...],
    coverage_bounds: tuple[float, float, float, float],
    tile_bytes: bytes,
    generated_at: datetime,
    package_id: str | None = None,
    version: str = "v1",
    tile_count: int | None = None,
    file_count: int | None = None,
    record_count: int | None = None,
) -> Path:
    provider_dir = root / provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    resolved_package_id = package_id or f"{provider}-offline-v1"
    actual_tile_count = tile_count if tile_count is not None else len(tiles)
    actual_record_count = record_count if record_count is not None else len(tiles)
    index_file = f"{provider}.{package_format}"
    if package_format == "mbtiles":
        actual_file_count = file_count if file_count is not None else 1
        write_mbtiles(
            provider_dir / index_file,
            tiles,
            tile_bytes,
        )
    elif package_format == "pmtiles":
        actual_file_count = file_count if file_count is not None else 1
        _write_pmtiles(
            provider_dir / index_file,
            tiles,
            tile_bytes,
        )
    else:
        raise ValueError(f"unsupported fixture package format: {package_format}")

    payload = manifest_payload(
        provider=provider,
        package_id=resolved_package_id,
        version=version,
        generated_at=generated_at,
        max_age_days=30,
        coverage_bounds=coverage_bounds,
        zoom_levels=zoom_levels,
        tile_format="png",
        source_statement=f"{provider} offline basemap fixture",
        package_format=package_format,
        index_file=index_file,
        tile_count=actual_tile_count,
        file_count=actual_file_count,
        record_count=actual_record_count,
    )
    payload["checksum"] = manifest_checksum(payload)
    (provider_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return provider_dir
