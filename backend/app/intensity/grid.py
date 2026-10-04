from __future__ import annotations

import hashlib
import json
import math

import numpy as np
from pyproj import CRS, Geod, Transformer

from app.intensity.domain import GridDefinition, GridSamples


def grid_definition_from_bounds(
    *,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    resolution_m: int,
    crs: str,
    version: str,
) -> GridDefinition:
    if resolution_m <= 0:
        raise ValueError("resolution_m must be positive")
    if min_x > max_x or min_y > max_y:
        raise ValueError("grid bounds must be ordered")
    origin_x = math.floor(min_x / resolution_m) * resolution_m
    aligned_min_y = math.floor(min_y / resolution_m) * resolution_m
    origin_y = math.ceil(max_y / resolution_m) * resolution_m
    width = math.ceil((max_x - origin_x) / resolution_m)
    height = math.ceil((origin_y - aligned_min_y) / resolution_m)
    return GridDefinition(
        version=version,
        crs=crs,
        resolution_m=resolution_m,
        origin_x=float(origin_x),
        origin_y=float(origin_y),
        width=width,
        height=height,
    )


def grid_definition_checksum(definition: GridDefinition) -> str:
    payload = {
        "version": definition.version,
        "crs": definition.crs,
        "resolution_m": definition.resolution_m,
        "origin_x": definition.origin_x,
        "origin_y": definition.origin_y,
        "width": definition.width,
        "height": definition.height,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def sample_grid(
    definition: GridDefinition,
    *,
    epicenter_longitude: float,
    epicenter_latitude: float,
) -> GridSamples:
    rows, columns = np.indices((definition.height, definition.width))
    rows = rows.reshape(-1)
    columns = columns.reshape(-1)
    projected_x = definition.origin_x + (columns + 0.5) * definition.resolution_m
    projected_y = definition.origin_y - (rows + 0.5) * definition.resolution_m

    project_to_wgs84 = Transformer.from_crs(
        CRS.from_user_input(definition.crs),
        CRS.from_epsg(4326),
        always_xy=True,
    )
    longitude, latitude = project_to_wgs84.transform(projected_x, projected_y)
    longitude = np.asarray(longitude, dtype=np.float64)
    latitude = np.asarray(latitude, dtype=np.float64)

    geod = Geod(ellps="WGS84")
    azimuth_deg, _, distance_m = geod.inv(
        np.full(longitude.shape, epicenter_longitude, dtype=np.float64),
        np.full(latitude.shape, epicenter_latitude, dtype=np.float64),
        longitude,
        latitude,
    )
    project_epicenter = Transformer.from_crs(
        CRS.from_epsg(4326),
        CRS.from_user_input(definition.crs),
        always_xy=True,
    )
    epicenter_x, epicenter_y = project_epicenter.transform(
        epicenter_longitude,
        epicenter_latitude,
    )
    return GridSamples(
        definition=definition,
        rows=rows,
        columns=columns,
        x_km=(projected_x - epicenter_x) / 1000.0,
        y_km=(projected_y - epicenter_y) / 1000.0,
        longitude=longitude,
        latitude=latitude,
        distance_km=np.asarray(distance_m, dtype=np.float64) / 1000.0,
        azimuth_deg=np.asarray(azimuth_deg, dtype=np.float64),
    )
