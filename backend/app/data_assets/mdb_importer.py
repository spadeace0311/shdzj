from __future__ import annotations

import base64
import math
import struct
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from shapely.geometry import (
    LineString,
    LinearRing,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.validation import explain_validity
from shapely import wkt as shapely_wkt

from app.config import settings
from app.data_assets.domain import (
    DataAssetDefinition,
    NormalizedRecord,
    NormalizedTableData,
)


SUPPORTED_SHAPE_TYPES = {
    1: "POINT",
    3: "POLYLINE",
    5: "POLYGON",
    8: "MULTIPOINT",
    11: "POINT_Z",
    13: "POLYLINE_Z",
    15: "POLYGON_Z",
    18: "MULTIPOINT_Z",
    21: "POINT_M",
    23: "POLYLINE_M",
    25: "POLYGON_M",
    28: "MULTIPOINT_M",
}


class MdbConnectionFactory:
    def __init__(self, driver: str) -> None:
        self._driver = driver

    def connect(self, path: Path):
        import pyodbc

        connection_string = (
            f"DRIVER={{{self._driver}}};"
            f"DBQ={path.resolve()};"
            "ReadOnly=True;"
        )
        return pyodbc.connect(connection_string, autocommit=False)


class MdbAssetImporter:
    def __init__(self, connection_factory=None) -> None:
        self._connection_factory = connection_factory or MdbConnectionFactory(
            settings.data_asset_mdb_driver
        )

    def load(
        self,
        path: Path,
        definition: DataAssetDefinition,
    ) -> NormalizedTableData:
        if definition.source_table is None:
            raise ValueError("data asset does not define an MDB source table")
        connection = self._connection_factory.connect(path)
        try:
            cursor = connection.cursor()
            cursor.execute(f"SELECT * FROM [{definition.source_table}]")
            rows = cursor.fetchall()
            columns = tuple(item[0] for item in cursor.description)
            records_list: list[NormalizedRecord] = []
            seen: set[str] = set()
            for index, row in enumerate(rows, start=1):
                record = self._normalize_row(
                    row_number=index,
                    columns=columns,
                    row=row,
                    definition=definition,
                )
                if record.business_key in definition.contract.excluded_business_keys:
                    continue
                if record.business_key in seen:
                    raise ValueError(
                        f"business key is duplicated at row {index}"
                    )
                seen.add(record.business_key)
                records_list.append(record)
            records = tuple(records_list)
            return NormalizedTableData(
                columns=columns,
                records=records,
                source_crs=definition.contract.source_crs,
                spatial_extent=_extent(records),
            )
        finally:
            connection.close()

    @staticmethod
    def _normalize_row(
        *,
        row_number: int,
        columns: tuple[str, ...],
        row,
        definition: DataAssetDefinition,
    ) -> NormalizedRecord:
        contract = definition.contract
        column_lookup = {
            str(column).upper(): str(column)
            for column in columns
        }
        contract_lookup = {
            field.name.upper(): field
            for field in contract.fields
        }
        properties: dict[str, object] = {}
        shape_value = None
        has_shape_field = "SHAPE" in column_lookup

        for column, value in zip(columns, row, strict=True):
            if str(column).upper() == "SHAPE":
                shape_value = value
                continue
            field = contract_lookup.get(str(column).upper())
            field_type = field.python_type if field is not None else None
            converted = _json_safe(value, field_type)
            properties[str(column)] = _coerce_contract_value(
                converted,
                field_type,
            )

        geometry_wkt = None
        if contract.geometry_type is not None:
            if not has_shape_field:
                raise ValueError("MDB row is missing the SHAPE geometry field")
            geometry = parse_esri_shape(shape_value)
            geometry_wkt = geometry.wkt
        elif has_shape_field and shape_value is not None:
            parse_esri_shape(shape_value)

        business_key = _business_key(
            properties,
            contract.business_key_fields,
            column_lookup,
        )
        return NormalizedRecord(
            row_number=row_number,
            business_key=business_key,
            properties=properties,
            geometry_wkt=geometry_wkt,
        )


def parse_esri_shape(blob: bytes) -> BaseGeometry:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError("ESRI shape must be a binary value")
    buffer = bytes(blob)
    if len(buffer) < 4:
        raise ValueError("ESRI shape blob is missing its type header")
    shape_type = struct.unpack_from("<i", buffer, 0)[0]
    if shape_type == 0:
        raise ValueError("ESRI null shape is not supported")
    if shape_type not in SUPPORTED_SHAPE_TYPES:
        raise ValueError(f"unsupported ESRI shape type: {shape_type}")

    if shape_type in {1, 11, 21}:
        return _point(buffer)
    if shape_type in {3, 13, 23}:
        return _polyline(buffer)
    if shape_type in {5, 15, 25}:
        return _polygon(buffer)
    if shape_type in {8, 18, 28}:
        return _multipoint(buffer)
    raise ValueError(f"unsupported ESRI shape type: {shape_type}")


def _point(buffer: bytes) -> Point:
    if len(buffer) < 20:
        raise ValueError("truncated ESRI point shape")
    x, y = struct.unpack_from("<2d", buffer, 4)
    _require_finite(x, y)
    return Point(x, y)


def _multipoint(buffer: bytes) -> MultiPoint:
    if len(buffer) < 40:
        raise ValueError("truncated ESRI multipoint shape")
    num_points = struct.unpack_from("<i", buffer, 36)[0]
    _require_shape_counts(num_points)
    offset = 40
    coordinates = _read_coordinates(buffer, offset, num_points)
    geometry = MultiPoint(coordinates)
    _require_valid(geometry, "multipoint")
    return geometry


def _polyline(buffer: bytes) -> MultiLineString:
    header = _read_poly_header(buffer)
    parts, coordinates = _read_parts_and_coordinates(
        buffer,
        header["num_parts"],
        header["num_points"],
    )
    lines = []
    for part in parts:
        if len(part) < 2:
            raise ValueError("ESRI polyline part must contain at least two points")
        lines.append(LineString(part))
    geometry = MultiLineString(lines)
    _require_valid(geometry, "polyline")
    return geometry


def _polygon(buffer: bytes) -> MultiPolygon:
    header = _read_poly_header(buffer)
    parts, coordinates = _read_parts_and_coordinates(
        buffer,
        header["num_parts"],
        header["num_points"],
    )
    rings: list[LinearRing] = []
    for part in parts:
        if len(part) < 4:
            raise ValueError("ESRI polygon ring must contain at least four points")
        if part[0] != part[-1]:
            raise ValueError("ESRI polygon ring is not closed")
        ring = LinearRing(part)
        if ring.area == 0:
            raise ValueError("ESRI polygon ring has zero area")
        rings.append(ring)
    polygons = _group_polygon_rings(rings)
    geometry = MultiPolygon(polygons)
    _require_valid(geometry, "polygon")
    return geometry


def _read_poly_header(buffer: bytes) -> dict[str, int]:
    if len(buffer) < 44:
        raise ValueError("truncated ESRI polyline or polygon shape")
    num_parts, num_points = struct.unpack_from("<2i", buffer, 36)
    _require_shape_counts(num_points)
    if num_parts < 1:
        raise ValueError("ESRI polyline or polygon shape has no parts")
    return {"num_parts": num_parts, "num_points": num_points}


def _read_parts_and_coordinates(
    buffer: bytes,
    num_parts: int,
    num_points: int,
) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]:
    offset = 44
    part_values, offset = _unpack_from(
        buffer,
        offset,
        f"<{num_parts}i",
        "part indices",
    )
    if part_values != tuple(sorted(part_values)):
        raise ValueError("ESRI shape part indices are not ordered")
    if any(part < 0 or part >= num_points for part in part_values):
        raise ValueError("ESRI shape part index is out of range")

    raw_coordinates, _ = _unpack_from(
        buffer,
        offset,
        f"<{num_points * 2}d",
        "coordinates",
    )
    coordinates = tuple(
        (raw_coordinates[index], raw_coordinates[index + 1])
        for index in range(0, num_points * 2, 2)
    )
    for x, y in coordinates:
        _require_finite(x, y)

    parts = []
    for part_index, start in enumerate(part_values):
        end = (
            part_values[part_index + 1]
            if part_index + 1 < len(part_values)
            else num_points
        )
        parts.append(coordinates[start:end])
    return tuple(parts), coordinates


def _read_coordinates(
    buffer: bytes,
    offset: int,
    num_points: int,
) -> tuple[tuple[float, float], ...]:
    raw_coordinates, _ = _unpack_from(
        buffer,
        offset,
        f"<{num_points * 2}d",
        "coordinates",
    )
    coordinates = tuple(
        (raw_coordinates[index], raw_coordinates[index + 1])
        for index in range(0, num_points * 2, 2)
    )
    for x, y in coordinates:
        _require_finite(x, y)
    return coordinates


def _unpack_from(
    buffer: bytes,
    offset: int,
    format_string: str,
    label: str,
) -> tuple[tuple, int]:
    size = struct.calcsize(format_string)
    if offset + size > len(buffer):
        raise ValueError(f"truncated ESRI shape {label}")
    return struct.unpack_from(format_string, buffer, offset), offset + size


def _require_shape_counts(num_points: int) -> None:
    if num_points < 1:
        raise ValueError("ESRI shape contains no points")


def _require_finite(*values: float) -> None:
    if any(not math.isfinite(float(value)) for value in values):
        raise ValueError("ESRI shape contains non-finite coordinates")


def _require_valid(geometry: BaseGeometry, label: str) -> None:
    if not geometry.is_valid:
        raise ValueError(
            f"invalid ESRI {label} geometry: {explain_validity(geometry)}"
        )


def _group_polygon_rings(rings: list[LinearRing]) -> list[Polygon]:
    exteriors = [ring for ring in rings if not ring.is_ccw]
    holes = [ring for ring in rings if ring.is_ccw]
    if not exteriors:
        exteriors = [max(rings, key=lambda ring: abs(ring.area))]
        holes = [ring for ring in rings if ring is not exteriors[0]]

    polygons: list[Polygon] = []
    assigned_holes: set[int] = set()
    for exterior in exteriors:
        exterior_polygon = Polygon(exterior.coords)
        polygon_holes = []
        for index, hole in enumerate(holes):
            if index in assigned_holes:
                continue
            if exterior_polygon.covers(Point(hole.coords[0])):
                polygon_holes.append(hole)
                assigned_holes.add(index)
        polygon = Polygon(exterior.coords, [hole.coords for hole in polygon_holes])
        if not polygon.is_valid:
            raise ValueError(
                f"invalid ESRI polygon ring: {explain_validity(polygon)}"
            )
        polygons.append(orient(polygon, sign=1.0))
    if len(assigned_holes) != len(holes):
        raise ValueError("ESRI polygon has a hole outside every exterior ring")
    return polygons


def _json_safe(value: object, field_type: str | None) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if field_type == "number":
            return float(value)
        if field_type == "integer":
            if value == value.to_integral_value():
                return int(value)
            return float(value)
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return value


def _coerce_contract_value(value: object, field_type: str | None) -> object:
    if value is None:
        return None
    if field_type == "number":
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                return None
        if isinstance(value, Decimal):
            return float(value)
    if field_type == "integer":
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return int(Decimal(text))
            except (InvalidOperation, ValueError):
                return None
        if isinstance(value, Decimal):
            return int(value)
    return value


def _business_key(
    properties: dict[str, object],
    fields: tuple[str, ...],
    column_lookup: dict[str, str],
) -> str:
    values = []
    for field in fields:
        column = column_lookup.get(field.upper())
        if column is None:
            raise ValueError(f"business key field is missing from MDB row: {field}")
        value = properties.get(column)
        text = "" if value is None else str(value).strip()
        if not text:
            raise ValueError("business key fields are required")
        values.append(text)
    return "|".join(values)


def _extent(
    records: tuple[NormalizedRecord, ...],
) -> tuple[float, float, float, float] | None:
    geometries = [
        shapely_wkt.loads(record.geometry_wkt)
        for record in records
        if record.geometry_wkt
    ]
    if not geometries:
        return None
    merged = geometries[0]
    for geometry in geometries[1:]:
        merged = merged.union(geometry)
    min_x, min_y, max_x, max_y = merged.bounds
    return (min_x, min_y, max_x, max_y)
