from __future__ import annotations

import json
from pathlib import Path

from pyproj import Transformer
from shapely import wkt as shapely_wkt
from shapely.geometry import MultiPolygon, shape
from shapely.ops import transform
from shapely.validation import explain_validity

from app.data_assets.domain import (
    DataAssetDefinition,
    NormalizedRecord,
    NormalizedTableData,
)


class GeoJsonAssetImporter:
    def load(
        self,
        path: Path,
        definition: DataAssetDefinition,
    ) -> NormalizedTableData:
        document = json.loads(path.read_text(encoding="utf-8"))
        features = self._features(document)
        contract = definition.contract
        transformer = (
            None
            if contract.source_crs == "EPSG:4326"
            else Transformer.from_crs(contract.source_crs, "EPSG:4326", always_xy=True).transform
        )
        records: list[NormalizedRecord] = []
        seen: set[str] = set()
        columns: set[str] = set()

        for row_number, feature in enumerate(features, start=1):
            properties = dict(feature.get("properties") or {})
            columns.update(properties)
            business_key = self._business_key(properties, contract.business_key_fields)
            if business_key in seen:
                raise ValueError(f"business key is duplicated at row {row_number}")
            seen.add(business_key)
            geometry = None
            geometry_wkt = None
            if feature.get("geometry") is not None:
                geometry = shape(feature["geometry"])
                if not geometry.is_valid:
                    raise ValueError(f"invalid geometry at row {row_number}: {explain_validity(geometry)}")
                if transformer is not None:
                    geometry = transform(transformer, geometry)
                if geometry.geom_type == "Polygon":
                    geometry = MultiPolygon([geometry])
                geometry_wkt = geometry.wkt
            records.append(
                NormalizedRecord(
                    row_number=row_number,
                    business_key=business_key,
                    properties=properties,
                    geometry_wkt=geometry_wkt,
                )
            )

        extent = None
        if any(record.geometry_wkt for record in records):
            geometries = [
                shapely_wkt.loads(record.geometry_wkt)
                for record in records
                if record.geometry_wkt
            ]
            merged = geometries[0]
            for geometry in geometries[1:]:
                merged = merged.union(geometry)
            min_x, min_y, max_x, max_y = merged.bounds
            extent = (min_x, min_y, max_x, max_y)
        return NormalizedTableData(
            columns=tuple(sorted(columns)),
            records=tuple(records),
            source_crs="EPSG:4326",
            spatial_extent=extent,
        )

    @staticmethod
    def _features(document: object) -> list[dict]:
        if not isinstance(document, dict):
            raise ValueError("GeoJSON root must be an object")
        if document.get("type") == "FeatureCollection":
            features = document.get("features")
            if not isinstance(features, list):
                raise ValueError("GeoJSON features must be a list")
            return features
        if document.get("type") == "Feature":
            return [document]
        raise ValueError("GeoJSON must be a Feature or FeatureCollection")

    @staticmethod
    def _business_key(properties: dict, fields: tuple[str, ...]) -> str:
        values = [str(properties.get(field, "")).strip() for field in fields]
        if not values or any(not value for value in values):
            raise ValueError("business key fields are required")
        return "|".join(values)
