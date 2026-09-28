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
            srid = _resolve_srid(CRS.from_user_input(dataset.crs))
            if srid is None:
                raise ValueError(
                    "GeoTIFF CRS must map to a numeric EPSG or ESRI code"
                )
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
                srid=srid,
                band_count=dataset.count,
                dtype=dataset.dtypes[0],
                nodata=dataset.nodata,
                resolution_x=resolution_x,
                resolution_y=resolution_y,
                spatial_extent=tuple(dataset.bounds),
            )


def _resolve_srid(crs: CRS) -> int | None:
    epsg = crs.to_epsg()
    if epsg is not None:
        return epsg
    authority = crs.to_authority()
    if authority is None:
        return None
    auth_name, auth_code = authority
    if auth_name.upper() not in {"EPSG", "ESRI"} or not auth_code.isdigit():
        return None
    return int(auth_code)
