import struct
from pathlib import Path

import pytest

from app.data_assets.mdb_importer import MdbAssetImporter, parse_esri_shape
from app.data_assets.registry import get_asset_definition


def _point_shape(x: float, y: float) -> bytes:
    return struct.pack("<i2d", 1, x, y)


def _point_z_shape(
    x: float,
    y: float,
    z: float,
    m: float | None = None,
) -> bytes:
    if m is None:
        return struct.pack("<i3d", 11, x, y, z)
    return struct.pack("<i4d", 11, x, y, z, m)


def _point_m_shape(x: float, y: float, m: float | None = None) -> bytes:
    if m is None:
        return struct.pack("<i2d", 21, x, y)
    return struct.pack("<i3d", 21, x, y, m)


def _poly_shape(
    shape_type: int,
    points: tuple[tuple[float, float], ...],
    parts: tuple[int, ...],
    *,
    zs: tuple[float, ...] | None = None,
    ms: tuple[float, ...] | None = None,
) -> bytes:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    header = struct.pack(
        "<i4dii",
        shape_type,
        min(xs),
        min(ys),
        max(xs),
        max(ys),
        len(parts),
        len(points),
    )
    parts_blob = struct.pack(f"<{len(parts)}i", *parts)
    coordinates = struct.pack(
        f"<{len(points) * 2}d",
        *(coordinate for point in points for coordinate in point),
    )
    payload = header + parts_blob + coordinates
    if zs is not None:
        payload += struct.pack("<2d", min(zs), max(zs))
        payload += struct.pack(f"<{len(zs)}d", *zs)
    if ms is not None:
        payload += struct.pack("<2d", min(ms), max(ms))
        payload += struct.pack(f"<{len(ms)}d", *ms)
    return payload


def _polyline_shape(points: tuple[tuple[float, float], ...]) -> bytes:
    return _poly_shape(3, points, (0,))


def _polygon_shape(ring: tuple[tuple[float, float], ...]) -> bytes:
    return _poly_shape(5, ring, (0,))


def _multipoint_shape(
    points: tuple[tuple[float, float], ...],
    *,
    shape_type: int = 8,
    zs: tuple[float, ...] | None = None,
    ms: tuple[float, ...] | None = None,
) -> bytes:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    header = struct.pack(
        "<i4di",
        shape_type,
        min(xs),
        min(ys),
        max(xs),
        max(ys),
        len(points),
    )
    coordinates = struct.pack(
        f"<{len(points) * 2}d",
        *(coordinate for point in points for coordinate in point),
    )
    payload = header + coordinates
    if zs is not None:
        payload += struct.pack("<2d", min(zs), max(zs))
        payload += struct.pack(f"<{len(zs)}d", *zs)
    if ms is not None:
        payload += struct.pack("<2d", min(ms), max(ms))
        payload += struct.pack(f"<{len(ms)}d", *ms)
    return payload


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

    def close(self) -> None:
        return None


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


def test_mdb_importer_canonicalizes_contract_field_names(tmp_path: Path) -> None:
    source = tmp_path / "base.mdb"
    source.write_bytes(b"not-opened-by-fake")

    class CanonicalCursor:
        description = [("id",), ("name",), ("SHAPE",)]
        rows = [
            (
                "T001",
                "测试镇",
                _polygon_shape(
                    ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 0.0))
                ),
            )
        ]

        def execute(self, statement: str) -> None:
            assert statement == "SELECT * FROM [TOWN_CODE]"

        def fetchall(self):
            return self.rows

        def close(self) -> None:
            return None

    class CanonicalConnection:
        def cursor(self) -> CanonicalCursor:
            return CanonicalCursor()

        def close(self) -> None:
            return None

    class CanonicalFactory:
        def connect(self, _path):
            return CanonicalConnection()

    importer = MdbAssetImporter(connection_factory=CanonicalFactory())
    result = importer.load(source, get_asset_definition("shanghai.admin.town"))

    assert result.records[0].properties["ID"] == "T001"
    assert result.records[0].properties["NAME"] == "测试镇"
    assert "id" not in result.records[0].properties


def test_parse_esri_polygon_shape() -> None:
    ring = ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 0.0))

    geometry = parse_esri_shape(_polygon_shape(ring))

    assert geometry.geom_type == "MultiPolygon"
    assert geometry.area == pytest.approx(2.0)


def test_parse_esri_multipoint_shape() -> None:
    geometry = parse_esri_shape(
        _multipoint_shape(((0.0, 0.0), (1.0, 1.0), (2.0, 0.0)))
    )

    assert geometry.geom_type == "MultiPoint"
    assert len(geometry.geoms) == 3


@pytest.mark.parametrize(
    "blob",
    [
        _point_z_shape(121.5, 31.2, 10.0, 20.0),
        _point_m_shape(121.5, 31.2, 20.0),
    ],
)
def test_parse_esri_point_z_and_m_variants(blob: bytes) -> None:
    geometry = parse_esri_shape(blob)

    assert geometry.geom_type == "Point"
    assert geometry.x == pytest.approx(121.5)
    assert geometry.y == pytest.approx(31.2)


def test_parse_esri_polyline_z_and_m_arrays() -> None:
    points = ((0.0, 0.0), (1.0, 1.0))
    blob = _poly_shape(
        13,
        points,
        (0,),
        zs=(2.0, 3.0),
        ms=(4.0, 5.0),
    )

    geometry = parse_esri_shape(blob)

    assert geometry.geom_type == "MultiLineString"
    assert len(geometry.geoms) == 1


def test_parse_esri_rejects_null_shape() -> None:
    with pytest.raises(ValueError, match="null"):
        parse_esri_shape(struct.pack("<i", 0))


def test_parse_esri_rejects_wrong_byte_order() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        parse_esri_shape(struct.pack(">i2d", 1, 0.0, 0.0))


def test_parse_esri_rejects_unsupported_type() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        parse_esri_shape(struct.pack("<i", 999))


def test_parse_esri_rejects_truncated_point() -> None:
    blob = struct.pack("<i2d", 1, 0.0, 0.0)[:-1]

    with pytest.raises(ValueError, match="truncated"):
        parse_esri_shape(blob)


def test_parse_esri_rejects_invalid_polygon_ring() -> None:
    ring = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))

    with pytest.raises(ValueError, match="closed"):
        parse_esri_shape(_polygon_shape(ring))


def test_parse_esri_repairs_self_intersecting_polygon_ring() -> None:
    ring = (
        (0.0, 0.0),
        (2.0, 0.0),
        (1.0, 1.0),
        (2.0, 2.0),
        (0.0, 2.0),
        (1.0, 1.0),
        (0.0, 0.0),
    )

    geometry = parse_esri_shape(_polygon_shape(ring))

    assert geometry.geom_type == "MultiPolygon"
    assert geometry.is_valid
    assert len(geometry.geoms) == 2
    assert geometry.area == pytest.approx(2.0)


def test_parse_esri_rejects_malformed_part_indices() -> None:
    blob = _poly_shape(3, ((0.0, 0.0), (1.0, 1.0)), (1,))

    with pytest.raises(ValueError, match="part"):
        parse_esri_shape(blob)


def test_parse_esri_rejects_truncated_z_record() -> None:
    blob = _poly_shape(
        13,
        ((0.0, 0.0), (1.0, 1.0)),
        (0,),
        zs=(2.0, 3.0),
    )

    with pytest.raises(ValueError, match="record length"):
        parse_esri_shape(blob[:-1])


def test_mdb_importer_rejects_contract_geometry_mismatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "base.mdb"
    source.write_bytes(b"not-opened-by-fake")

    class PointCursor:
        description = [("OBJECTID",), ("name",), ("SHAPE",)]
        rows = [(1, "point feature", _point_shape(121.5, 31.2))]

        def execute(self, statement: str) -> None:
            assert statement == "SELECT * FROM [ACTIVEFAULT]"

        def fetchall(self):
            return self.rows

        def close(self) -> None:
            return None

    class PointConnection:
        def cursor(self) -> PointCursor:
            return PointCursor()

        def close(self) -> None:
            return None

    class PointConnectionFactory:
        def connect(self, _path):
            return PointConnection()

    importer = MdbAssetImporter(connection_factory=PointConnectionFactory())

    with pytest.raises(ValueError, match="geometry type"):
        importer.load(source, get_asset_definition("shanghai.fault"))


def test_mdb_importer_rejects_duplicate_business_key(tmp_path: Path) -> None:
    source = tmp_path / "base.mdb"
    source.write_bytes(b"not-opened-by-fake")

    class DuplicateCursor:
        description = [("OBJECTID",), ("name",), ("SHAPE",)]
        rows = [
            (1, "duplicate", _polyline_shape(((0.0, 0.0), (1.0, 1.0)))),
            (1, "duplicate", _polyline_shape(((2.0, 2.0), (3.0, 3.0)))),
        ]

        def execute(self, statement: str) -> None:
            assert statement == "SELECT * FROM [ACTIVEFAULT]"

        def fetchall(self):
            return self.rows

        def close(self) -> None:
            return None

    class DuplicateConnection:
        def cursor(self) -> DuplicateCursor:
            return DuplicateCursor()

        def close(self) -> None:
            return None

    class DuplicateConnectionFactory:
        def connect(self, _path):
            return DuplicateConnection()

    importer = MdbAssetImporter(connection_factory=DuplicateConnectionFactory())

    with pytest.raises(ValueError, match="business key"):
        importer.load(source, get_asset_definition("shanghai.fault"))


def test_population_importer_excludes_district_aggregate(tmp_path: Path) -> None:
    source = tmp_path / "population.mdb"
    source.write_bytes(b"not-opened-by-fake")
    rows = [
        ("31012000000000", "District aggregate", "100", "100", "0", "1", "2", "3")
    ]
    rows.extend(
        (
            f"310115{index:06d}",
            f"town-{index}",
            "100",
            "70",
            "20",
            "50",
            "10",
            "15",
        )
        for index in range(1, 213)
    )

    class PopulationCursor:
        description = [
            ("ID",),
            ("NAME",),
            ("total",),
            ("resident",),
            ("floating",),
            ("family",),
            ("under14",),
            ("over65",),
        ]

        def __init__(self) -> None:
            self.rows = rows

        def execute(self, statement: str) -> None:
            assert statement == "SELECT * FROM [TOWN_POPULATION]"

        def fetchall(self):
            return self.rows

        def close(self) -> None:
            return None

    class PopulationConnection:
        def cursor(self) -> PopulationCursor:
            return PopulationCursor()

        def close(self) -> None:
            return None

    class PopulationConnectionFactory:
        def connect(self, _path):
            return PopulationConnection()

    importer = MdbAssetImporter(
        connection_factory=PopulationConnectionFactory()
    )

    result = importer.load(
        source,
        get_asset_definition("shanghai.population.town"),
    )

    assert result.record_count == 212
    assert "31012000000000" not in {
        record.business_key for record in result.records
    }
