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
                tags = dataset.tags()
                band_manifest = json.loads(tags["band_manifest"]) if "band_manifest" in tags else {}
                transform = dataset.transform
                metadata = {
                    "crs": dataset.crs.to_string(),
                    "width": dataset.width,
                    "height": dataset.height,
                    "srid": dataset.crs.to_epsg(),
                    "origin_x": float(transform.c),
                    "origin_y": float(transform.f),
                    "resolution_m": float(transform.a),
                    "grid_definition_version": tags.get("grid_definition_version"),
                    "bands": band_manifest.get("bands"),
                }
        return bands, metadata

    @staticmethod
    def checksum(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def content_checksum(
        definition: GridDefinition,
        bands: Sequence[BandInput],
        band_manifest: dict,
    ) -> str:
        digest = hashlib.sha256(b"intensity-raster-content-v1\0")
        digest.update(
            json.dumps(
                _json_canonical(
                    {
                        "grid": {
                            "version": definition.version,
                            "crs": definition.crs,
                            "resolution_m": definition.resolution_m,
                            "origin_x": definition.origin_x,
                            "origin_y": definition.origin_y,
                            "width": definition.width,
                            "height": definition.height,
                        },
                        "manifest": band_manifest,
                    }
                ),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        for index, band in enumerate(bands, start=1):
            if isinstance(band, tuple):
                name, values = band
            else:
                values = band
                name = _band_name(band_manifest, index)
            values = np.ascontiguousarray(values, dtype=np.float64)
            digest.update(str(name).encode("utf-8"))
            digest.update(len(str(name).encode("utf-8")).to_bytes(4, "little"))
            digest.update(
                str((values.shape[0], values.shape[1])).encode("ascii")
            )
            digest.update(values.tobytes(order="C"))
        return digest.hexdigest()


def _band_name(manifest: dict, number: int) -> str:
    for band in manifest.get("bands", []):
        if band.get("number") == number:
            return str(band["name"])
    return f"band_{number}"


def _json_canonical(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {str(key): _json_canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_canonical(item) for item in value]
    return value
