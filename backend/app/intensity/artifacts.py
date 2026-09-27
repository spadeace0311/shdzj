from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import numpy as np
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from app.intensity.domain import GridDefinition


BandInput = tuple[str, np.ndarray] | np.ndarray


class RasterCodec:
    @staticmethod
    def encode(
        definition: GridDefinition,
        bands: Sequence[BandInput],
        band_manifest: dict,
    ) -> bytes:
        if not bands:
            raise ValueError("at least one raster band is required")

        normalized: list[tuple[str, np.ndarray]] = []
        expected = (definition.height, definition.width)
        for index, band in enumerate(bands, start=1):
            if isinstance(band, tuple):
                name, values = band
            else:
                values = band
                name = _band_name(band_manifest, index)
            values = np.asarray(values, dtype=np.float64)
            if values.shape != expected:
                raise ValueError("raster band shape does not match grid definition")
            normalized.append((name, values))

        transform = from_origin(
            definition.origin_x,
            definition.origin_y,
            definition.resolution_m,
            definition.resolution_m,
        )
        profile = {
            "driver": "GTiff",
            "height": definition.height,
            "width": definition.width,
            "count": len(normalized),
            "dtype": "float64",
            "crs": definition.crs,
            "transform": transform,
            "compress": "deflate",
        }
        with MemoryFile() as memory:
            with memory.open(**profile) as dataset:
                for index, (_, values) in enumerate(normalized, start=1):
                    dataset.write(values, index)
                dataset.update_tags(
                    grid_definition_version=definition.version,
                    band_manifest=json.dumps(
                        band_manifest,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            return memory.read()

    @staticmethod
    def decode(payload: bytes) -> tuple[list[np.ndarray], dict]:
        with MemoryFile(payload) as memory:
            with memory.open() as dataset:
                bands = [
                    dataset.read(index)
                    for index in range(1, dataset.count + 1)
                ]
                metadata = {
                    "crs": dataset.crs.to_string(),
                    "width": dataset.width,
                    "height": dataset.height,
                    "grid_definition_version": dataset.tags().get(
                        "grid_definition_version"
                    ),
                }
        return bands, metadata

    @staticmethod
    def checksum(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()


def _band_name(manifest: dict, number: int) -> str:
    for band in manifest.get("bands", []):
        if band.get("number") == number:
            return str(band["name"])
    return f"band_{number}"
