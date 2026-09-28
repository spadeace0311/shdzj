from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.data_assets.registry import get_asset_definition
from app.data_assets.raster_importer import GeoTiffAssetImporter


def test_geotiff_importer_reads_metadata(tmp_path: Path) -> None:
    source = tmp_path / "gdp.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=4,
        height=3,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        nodata=-9999.0,
        transform=from_origin(121.0, 31.5, 0.01, 0.01),
    ) as dataset:
        dataset.write(np.ones((1, 3, 4), dtype=np.float32))

    descriptor = GeoTiffAssetImporter().load(
        source,
        get_asset_definition("shanghai.gdp.raster"),
    )

    assert descriptor.width == 4
    assert descriptor.height == 3
    assert descriptor.srid == 4326
    assert descriptor.band_count == 1
    assert descriptor.nodata == -9999.0
    assert descriptor.resolution_x == pytest.approx(0.01)


def test_geotiff_importer_rejects_unknown_crs(tmp_path: Path) -> None:
    source = tmp_path / "bad.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=1,
        height=1,
        count=1,
        dtype="uint8",
        transform=from_origin(0, 1, 1, 1),
    ) as dataset:
        dataset.write(np.zeros((1, 1, 1), dtype=np.uint8))

    with pytest.raises(ValueError, match="CRS"):
        GeoTiffAssetImporter().load(
            source,
            get_asset_definition("shanghai.gdp.raster"),
        )
