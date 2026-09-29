import math

import numpy as np
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds, from_origin
from rasterio.warp import Resampling, reproject, transform_bounds

MIN_TILE_ZOOM = 0
MAX_TILE_ZOOM = 22
_TILE_SIZE = 256


def render_raster_tile(
    values: np.ndarray,
    metadata: dict,
    z: int,
    x: int,
    y: int,
) -> bytes:
    source = np.ascontiguousarray(values, dtype=np.float64)
    src_transform = from_origin(
        float(metadata["origin_x"]),
        float(metadata["origin_y"]),
        float(metadata["resolution_m"]),
        float(metadata["resolution_m"]),
    )
    west, south, east, north = _tile_bounds(z, x, y)
    dst_transform = from_bounds(
        west,
        south,
        east,
        north,
        _TILE_SIZE,
        _TILE_SIZE,
    )
    destination = np.empty(
        (1, _TILE_SIZE, _TILE_SIZE),
        dtype=np.float64,
    )
    reproject(
        source=np.expand_dims(source, axis=0),
        destination=destination,
        src_transform=src_transform,
        src_crs=str(metadata["crs"]),
        src_nodata=np.nan,
        dst_transform=dst_transform,
        dst_crs="EPSG:3857",
        dst_nodata=np.nan,
        resampling=Resampling.nearest,
    )
    tile_values = destination[0]
    finite = np.isfinite(tile_values)
    rgb = np.zeros((_TILE_SIZE, _TILE_SIZE, 3), dtype=np.uint8)
    if np.any(finite):
        present = tile_values[finite]
        value_min = float(np.min(present))
        value_max = float(np.max(present))
        if value_max > value_min:
            normalized = (tile_values[finite] - value_min) / (
                value_max - value_min
            )
        else:
            normalized = np.full(present.shape, 0.5, dtype=np.float64)
        rgb[finite] = _ramp_colors(normalized)
    with MemoryFile() as memory:
        with memory.open(
            driver="PNG",
            width=_TILE_SIZE,
            height=_TILE_SIZE,
            count=3,
            dtype="uint8",
        ) as dataset:
            dataset.write(rgb[:, :, 0], 1)
            dataset.write(rgb[:, :, 1], 2)
            dataset.write(rgb[:, :, 2], 3)
        return memory.read()


def _tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(
        math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n)))
    )
    south = math.degrees(
        math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n)))
    )
    west_merc, south_merc, east_merc, north_merc = transform_bounds(
        "EPSG:4326",
        "EPSG:3857",
        west,
        south,
        east,
        north,
    )
    return west_merc, south_merc, east_merc, north_merc


def _ramp_colors(normalized: np.ndarray) -> np.ndarray:
    stops = (
        (0.0, (13, 8, 135)),
        (0.25, (0, 120, 200)),
        (0.5, (60, 180, 75)),
        (0.75, (255, 220, 25)),
        (1.0, (215, 25, 28)),
    )
    colors = np.zeros((normalized.shape[0], 3), dtype=np.float64)
    for index in range(len(stops) - 1):
        start, start_color = stops[index]
        end, end_color = stops[index + 1]
        mask = (normalized >= start) & (normalized <= end)
        span = max(end - start, 1e-12)
        amount = (normalized[mask] - start) / span
        for channel in range(3):
            colors[mask, channel] = (
                start_color[channel]
                + amount * (end_color[channel] - start_color[channel])
            )
    return np.clip(colors, 0, 255).astype(np.uint8)
