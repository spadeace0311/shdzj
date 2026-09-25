import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.regions.models import RegionBoundary


@dataclass(frozen=True, slots=True)
class ImportResult:
    version: str
    feature_count: int
    checksum: str
    activated: bool


async def import_geojson(
    session: AsyncSession,
    file_path: str | Path,
    *,
    version: str,
    name: str,
    activate: bool = False,
) -> ImportResult:
    source = Path(file_path)
    raw = source.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    normalized_version, normalized_name = _validate_metadata(version, name)
    document = _load_document(raw)
    polygons, feature_count = _extract_polygons(document)
    wkt = _multipolygon_wkt(polygons)

    if activate:
        await session.execute(update(RegionBoundary).values(is_active=False))

    await session.execute(
        insert(RegionBoundary).values(
            version=normalized_version,
            name=normalized_name,
            local_buffer_km=Decimal("50"),
            geom=func.ST_GeomFromText(wkt, 4326),
            source_uri=str(source),
            checksum=checksum,
            is_active=activate,
        )
    )

    return ImportResult(
        version=normalized_version,
        feature_count=feature_count,
        checksum=checksum,
        activated=activate,
    )


def _validate_metadata(version: str, name: str) -> tuple[str, str]:
    if not isinstance(version, str) or not version.strip():
        raise ValueError("version must not be empty")
    if len(version) > 64:
        raise ValueError("version must be at most 64 characters")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must not be empty")
    if len(name) > 128:
        raise ValueError("name must be at most 128 characters")
    return version.strip(), name.strip()


def _load_document(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("GeoJSON must be UTF-8 encoded") from exc

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid GeoJSON") from exc
    if not isinstance(document, dict):
        raise ValueError("GeoJSON root must be an object")
    return document


def _extract_polygons(document: dict[str, Any]) -> tuple[list[list[list[list[Decimal]]]], int]:
    document_type = document.get("type")
    if document_type == "FeatureCollection":
        features = document.get("features")
        if not isinstance(features, list):
            raise ValueError("FeatureCollection features must be a list")
        polygons: list[list[list[list[Decimal]]]] = []
        for feature in features:
            if not isinstance(feature, dict) or feature.get("type") != "Feature":
                raise ValueError("FeatureCollection entries must be Feature objects")
            polygons.extend(_geometry_polygons(feature.get("geometry")))
        return polygons, len(features)

    if document_type == "Feature":
        return _geometry_polygons(document.get("geometry")), 1

    return _geometry_polygons(document), 1


def _geometry_polygons(
    geometry: object,
) -> list[list[list[list[Decimal]]]]:
    if not isinstance(geometry, dict):
        raise ValueError("geometry is empty")

    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        return [_validate_polygon(coordinates)]
    if geometry_type == "MultiPolygon":
        if not isinstance(coordinates, list) or not coordinates:
            raise ValueError("geometry is empty")
        return [_validate_polygon(polygon) for polygon in coordinates]
    raise ValueError("geometry must be Polygon or MultiPolygon")


def _validate_polygon(coordinates: object) -> list[list[list[Decimal]]]:
    if not isinstance(coordinates, list) or not coordinates:
        raise ValueError("geometry is empty")

    polygon: list[list[list[Decimal]]] = []
    for ring in coordinates:
        if not isinstance(ring, list) or not ring:
            raise ValueError("polygon ring is empty")
        if len(ring) < 4:
            raise ValueError("polygon ring must contain at least four coordinates")
        polygon.append([_validate_position(position) for position in ring])
    return polygon


def _validate_position(position: object) -> list[Decimal]:
    if not isinstance(position, list) or len(position) < 2:
        raise ValueError("coordinate must contain longitude and latitude")
    if isinstance(position[0], bool) or isinstance(position[1], bool):
        raise ValueError("coordinate must contain numeric values")

    try:
        longitude = Decimal(str(position[0]))
        latitude = Decimal(str(position[1]))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("coordinate must contain numeric values") from exc

    if not longitude.is_finite() or longitude < Decimal("-180") or longitude > Decimal("180"):
        raise ValueError("longitude must be between -180 and 180")
    if not latitude.is_finite() or latitude < Decimal("-90") or latitude > Decimal("90"):
        raise ValueError("latitude must be between -90 and 90")
    return [longitude, latitude]


def _multipolygon_wkt(polygons: list[list[list[list[Decimal]]]]) -> str:
    if not polygons:
        raise ValueError("geometry is empty")

    polygon_text: list[str] = []
    for polygon in polygons:
        rings = [
            "(" + ", ".join(f"{position[0]} {position[1]}" for position in ring) + ")"
            for ring in polygon
        ]
        polygon_text.append("(" + ", ".join(rings) + ")")
    return "MULTIPOLYGON (" + ", ".join(polygon_text) + ")"
