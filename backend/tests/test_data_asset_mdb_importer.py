import struct
from pathlib import Path

import pytest

from app.data_assets.mdb_importer import MdbAssetImporter, parse_esri_shape
from app.data_assets.registry import get_asset_definition


def _point_shape(x: float, y: float) -> bytes:
    return struct.pack("<i2d", 1, x, y)


def _polyline_shape(points: tuple[tuple[float, float], ...]) -> bytes:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    header = struct.pack(
        "<i4dii",
        3,
        min(xs),
        min(ys),
        max(xs),
        max(ys),
        1,
        len(points),
    )
    parts = struct.pack("<i", 0)
    coordinates = struct.pack(
        f"<{len(points) * 2}d",
        *(coordinate for point in points for coordinate in point),
    )
    return header + parts + coordinates


class FakeCursor:
    def __init__(self) -> None:
        self.description = [
            ("OBJECTID",),
            ("name",),
            ("strike",),
            ("DIP_ANGLE",),
            ("DIP_DIR",),
            ("LENGTH",),
            ("WIDTH",),
            ("SHAPE",),
        ]
        self.rows = [
            (
                1,
                "断裂 A",
                "120.5",
                "30.0",
                "90",
                "10.0",
                "0.5",
                _polyline_shape(((121.5, 31.2), (121.6, 31.3))),
            ),
            (
                2,
                "断裂 B",
                "121.5",
                "31.0",
                "95",
                "12.0",
                "0.7",
                _polyline_shape(((121.7, 31.4), (121.8, 31.5))),
            ),
        ]

    def execute(self, statement: str) -> None:
        assert statement == "SELECT * FROM [ACTIVEFAULT]"

    def fetchall(self):
        return self.rows


class FakeConnection:
    def cursor(self) -> FakeCursor:
        return FakeCursor()

    def close(self) -> None:
        return None


def test_parse_esri_point_shape() -> None:
    geometry = parse_esri_shape(_point_shape(121.5, 31.2))

    assert geometry.geom_type == "Point"
    assert geometry.x == pytest.approx(121.5)
    assert geometry.y == pytest.approx(31.2)


def test_mdb_importer_normalizes_table_and_geometry(tmp_path: Path) -> None:
    source = tmp_path / "base.mdb"
    source.write_bytes(b"not-opened-by-fake")
    class FakeConnectionFactory:
        def connect(self, _path):
            return FakeConnection()

    importer = MdbAssetImporter(connection_factory=FakeConnectionFactory())

    result = importer.load(source, get_asset_definition("shanghai.fault"))

    assert result.record_count == 2
    assert result.records[0].business_key == "1"
    assert result.records[0].properties["name"] == "断裂 A"
    assert result.records[0].properties["strike"] == pytest.approx(120.5)
    assert result.records[0].geometry_wkt.startswith("MULTILINESTRING")
