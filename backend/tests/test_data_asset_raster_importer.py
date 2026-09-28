from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.coords import BoundingBox
from rasterio.transform import Affine

from app.data_assets.registry import get_asset_definition
from app.data_assets.raster_importer import GeoTiffAssetImporter


class _FakeDataset:
    def __init__(
        self,
        *,
        width=1,
        height=1,
        count=1,
        transform=None,
        bounds=None,
    ) -> None:
        self.crs = "EPSG:4326"
        self.width = width
        self.height = height
        self.count = count
        self.dtypes = ("float32",)
        self.nodata = None
        self.transform = transform or Affine(0.01, 0, 0, 0, -0.01, 0)
        self.bounds = bounds or BoundingBox(0, 0, 1, 1)


class _FakeOpen:
    def __init__(self, dataset: _FakeDataset) -> None:
        self.dataset = dataset

    def __enter__(self) -> _FakeDataset:
        return self.dataset

    def __exit__(self, *args: object) -> None:
        return None


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
        transform=Affine(0.01, 0, 121.0, 0, -0.01, 31.5),
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


def test_geotiff_importer_accepts_numeric_esri_authority(
    tmp_path: Path,
) -> None:
    source = tmp_path / "gdp-albers.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=1,
        height=1,
        count=1,
        dtype="float32",
        crs="ESRI:102025",
        transform=Affine(1000, 0, 0, 0, -1000, 0),
    ) as dataset:
        dataset.write(np.ones((1, 1, 1), dtype=np.float32))

    descriptor = GeoTiffAssetImporter().load(
        source,
        get_asset_definition("shanghai.gdp.raster"),
    )

    assert descriptor.srid == 102025


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
        transform=Affine(1, 0, 0, 0, -1, 1),
    ) as dataset:
        dataset.write(np.zeros((1, 1, 1), dtype=np.uint8))

    with pytest.raises(ValueError, match="CRS"):
        GeoTiffAssetImporter().load(
            source,
            get_asset_definition("shanghai.gdp.raster"),
        )


@pytest.mark.parametrize(
    ("dataset", "message"),
    [
        (
            _FakeDataset(width=0),
            "dimensions",
        ),
        (
            _FakeDataset(transform=Affine(float("inf"), 0, 0, 0, -0.01, 0)),
            "resolution",
        ),
        (
            _FakeDataset(transform=Affine(0, 0, 0, 0, 0, 0)),
            "resolution",
        ),
        (
            _FakeDataset(bounds=BoundingBox(1, 1, 1, 2)),
            "bounds",
        ),
        (
            _FakeDataset(bounds=BoundingBox(0, 0, 1, float("inf"))),
            "bounds",
        ),
    ],
)
def test_geotiff_importer_rejects_invalid_dataset(
    monkeypatch,
    dataset: _FakeDataset,
    message: str,
) -> None:
    monkeypatch.setattr(
        "app.data_assets.raster_importer.rasterio.open",
        lambda _path: _FakeOpen(dataset),
    )
    with pytest.raises(ValueError, match=message):
        GeoTiffAssetImporter().load(
            Path("fake.tif"),
            get_asset_definition("shanghai.gdp.raster"),
        )
