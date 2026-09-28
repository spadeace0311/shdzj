import json
from pathlib import Path

import pytest

from app.data_assets.geojson_importer import GeoJsonAssetImporter
from app.data_assets.registry import get_asset_definition


def test_geojson_importer_normalizes_polygon_and_fields(tmp_path: Path) -> None:
    source = tmp_path / "town.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"ID": "310115001", "NAME": "陆家嘴街道"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[121.50, 31.22], [121.51, 31.22], [121.51, 31.23], [121.50, 31.22]]
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = GeoJsonAssetImporter().load(
        source,
        get_asset_definition("shanghai.admin.town"),
    )

    assert result.record_count == 1
    assert result.records[0].business_key == "310115001"
    assert result.records[0].properties["NAME"] == "陆家嘴街道"
    assert result.records[0].geometry_wkt.startswith("MULTIPOLYGON")
    assert result.source_crs == "EPSG:4326"


def test_geojson_importer_rejects_duplicate_business_key(tmp_path: Path) -> None:
    source = tmp_path / "duplicate.geojson"
    feature = {
        "type": "Feature",
        "properties": {"ID": "same", "NAME": "重复"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[121.5, 31.2], [121.51, 31.2], [121.51, 31.21], [121.5, 31.2]]],
        },
    }
    source.write_text(
        json.dumps({"type": "FeatureCollection", "features": [feature, feature]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="business key"):
        GeoJsonAssetImporter().load(
            source,
            get_asset_definition("shanghai.admin.town"),
        )
