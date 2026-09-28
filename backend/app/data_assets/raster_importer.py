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
            if dataset.transform.a <= 0 or dataset.transform.e >= 0:
                raise ValueError("GeoTIFF resolution is invalid")
            if not dataset.bounds or not all(map(lambda value: value == value, dataset.bounds)):
                raise ValueError("GeoTIFF bounds are invalid")
            return NormalizedRasterData(
                width=dataset.width,
                height=dataset.height,
                srid=epsg,
                band_count=dataset.count,
                dtype=dataset.dtypes[0],
                nodata=dataset.nodata,
                resolution_x=abs(dataset.res[0]),
                resolution_y=abs(dataset.res[1]),
                spatial_extent=tuple(dataset.bounds),
            )
