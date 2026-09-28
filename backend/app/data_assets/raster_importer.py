from math import hypot, isclose, isfinite
from pathlib import Path

import rasterio
from pyproj import CRS

from app.data_assets.domain import DataAssetDefinition, NormalizedRasterData


class GeoTiffAssetImporter:
    def load(
        self,
        path: Path,
        definition: DataAssetDefinition,
    ) -> NormalizedRasterData:
        if definition.data_type.value != "raster":
            raise ValueError("GeoTIFF importer requires a raster asset")
        with rasterio.open(path) as dataset:
            if dataset.crs is None:
                raise ValueError("GeoTIFF CRS is required")
            epsg = CRS.from_user_input(dataset.crs).to_epsg()
            if epsg is None:
                raise ValueError("GeoTIFF CRS must map to an EPSG code")
            if dataset.width <= 0 or dataset.height <= 0 or dataset.count <= 0:
                raise ValueError("GeoTIFF dimensions and band count must be positive")
            transform = dataset.transform
            coefficients = (
                transform.a,
                transform.b,
                transform.c,
                transform.d,
                transform.e,
                transform.f,
            )
            if not all(isfinite(value) for value in coefficients):
                raise ValueError("GeoTIFF resolution is invalid")
            determinant = transform.a * transform.e - transform.b * transform.d
            resolution_x = hypot(transform.a, transform.b)
            resolution_y = hypot(transform.d, transform.e)
            if (
                resolution_x <= 0
                or resolution_y <= 0
                or isclose(determinant, 0.0, rel_tol=0.0, abs_tol=1e-15)
            ):
                raise ValueError("GeoTIFF resolution is invalid")
            bounds = dataset.bounds
            if (
                not bounds
                or not all(isfinite(value) for value in bounds)
                or bounds.left >= bounds.right
                or bounds.bottom >= bounds.top
            ):
                raise ValueError("GeoTIFF bounds are invalid")
            return NormalizedRasterData(
                width=dataset.width,
                height=dataset.height,
                srid=epsg,
                band_count=dataset.count,
                dtype=dataset.dtypes[0],
                nodata=dataset.nodata,
                resolution_x=resolution_x,
                resolution_y=resolution_y,
                spatial_extent=tuple(dataset.bounds),
            )
