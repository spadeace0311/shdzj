from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.qa.tools.event import load_context_event_and_revision
from app.qa.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    resolve_locked_asset_version,
)

HISTORICAL_EARTHQUAKE_ASSET_KEY = "shanghai.historical.earthquakes"

_POINT_EXPRESSION = """
    COALESCE(
      record.geom,
      CASE
        WHEN record.properties ? 'longitude'
          AND record.properties ? 'latitude'
        THEN ST_SetSRID(
          ST_MakePoint(
            (record.properties ->> 'longitude')::double precision,
            (record.properties ->> 'latitude')::double precision
          ),
          4326
        )
        ELSE NULL
      END
    )
"""

_WITHIN_RADIUS_SQL = text(
    f"""
    WITH candidates AS (
      SELECT
        record.business_key,
        record.properties,
        {_POINT_EXPRESSION} AS point_geom
      FROM data_asset_records AS record
      WHERE record.version_id = :version_id
    )
    SELECT
      business_key,
      properties,
      ST_Distance(
        point_geom::geography,
        ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)::geography
      ) / 1000.0 AS distance_km
    FROM candidates
    WHERE point_geom IS NOT NULL
      AND ST_DWithin(
        point_geom::geography,
        ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)::geography,
        :radius_m
      )
      AND COALESCE(
        NULLIF(properties ->> 'magnitude', '')::double precision,
        0
      ) >= :magnitude_min
    ORDER BY
      COALESCE(
        NULLIF(properties ->> 'magnitude', '')::double precision,
        0
      ) DESC,
      NULLIF(properties ->> 'origin_time', '')::timestamptz DESC NULLS LAST,
      business_key
    LIMIT 101
    """
)

_DISTANCE_SQL = text(
    f"""
    WITH locked_events AS (
      SELECT
        record.business_key,
        record.properties,
        {_POINT_EXPRESSION} AS point_geom
      FROM data_asset_records AS record
      WHERE record.version_id = :version_id
        AND (
          record.business_key = :historical_event_id
          OR record.properties ->> 'event_id' = :historical_event_id
        )
    ),
    selected AS (
      SELECT business_key, properties, point_geom
      FROM locked_events
      WHERE point_geom IS NOT NULL
      ORDER BY business_key
      LIMIT 1
    )
    SELECT
      business_key,
      properties,
      ST_Distance(
        point_geom::geography,
        ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)::geography
      ) / 1000.0 AS distance_km
    FROM selected
    """
)


class SeismicityWithinRadiusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID | None = None
    radius_km: Decimal = Field(default=Decimal("50"), gt=0, le=500)
    magnitude_min: Decimal = Field(default=Decimal("0"), ge=0, le=12)


class SeismicityDistanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID | None = None
    historical_event_id: str = Field(
        min_length=1,
        max_length=512,
        validation_alias=AliasChoices(
            "historical_event_id",
            "other_event_id",
        ),
    )


class SeismicityWithinRadiusTool:
    async def handle(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        radius_km = arguments.get("radius_km", Decimal("50"))
        magnitude_min = arguments.get("magnitude_min", Decimal("0"))
        event, revision, error = await load_context_event_and_revision(
            context,
            arguments.get("event_id"),
        )
        if error is not None:
            return error
        assert event is not None
        assert revision is not None
        version = await resolve_locked_asset_version(
            context,
            HISTORICAL_EARTHQUAKE_ASSET_KEY,
        )
        if version is None:
            return ToolResult.unavailable(
                limitations=("historical_earthquake_asset_missing",),
                source=HISTORICAL_EARTHQUAKE_ASSET_KEY,
                parameters={
                    "event_id": str(event.id),
                    "radius_km": float(radius_km),
                },
            )
        rows = (
            await context.session.execute(
                _WITHIN_RADIUS_SQL,
                {
                    "version_id": version.id,
                    "longitude": revision.longitude,
                    "latitude": revision.latitude,
                    "radius_m": float(radius_km) * 1000.0,
                    "magnitude_min": float(magnitude_min),
                },
            )
        ).mappings().all()
        truncated = len(rows) > 100
        events = [
            _historical_event_payload(
                business_key=row["business_key"],
                properties=dict(row["properties"] or {}),
                distance_km=row["distance_km"],
            )
            for row in rows[:100]
        ]
        return ToolResult.ok(
            value={
                "event_id": str(event.id),
                "radius_km": float(radius_km),
                "magnitude_min": float(magnitude_min),
                "events": events,
                "count": len(events),
                "matched_count": len(rows),
                "truncated": truncated,
            },
            unit="km",
            source=HISTORICAL_EARTHQUAKE_ASSET_KEY,
            version=version.version,
            limitations=("truncated_to_100",) if truncated else (),
            parameters={
                "event_id": str(event.id),
                "radius_km": float(radius_km),
                "magnitude_min": float(magnitude_min),
            },
        )


class SeismicityDistanceTool:
    async def handle(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        event, revision, error = await load_context_event_and_revision(
            context,
            arguments.get("event_id"),
        )
        if error is not None:
            return error
        assert event is not None
        assert revision is not None
        version = await resolve_locked_asset_version(
            context,
            HISTORICAL_EARTHQUAKE_ASSET_KEY,
        )
        if version is None:
            return ToolResult.unavailable(
                limitations=("historical_earthquake_asset_missing",),
                source=HISTORICAL_EARTHQUAKE_ASSET_KEY,
                parameters={
                    "event_id": str(event.id),
                    "historical_event_id": arguments["historical_event_id"],
                },
            )
        row = (
            await context.session.execute(
                _DISTANCE_SQL,
                {
                    "version_id": version.id,
                    "historical_event_id": arguments["historical_event_id"],
                    "longitude": revision.longitude,
                    "latitude": revision.latitude,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            return ToolResult.not_found(
                source=HISTORICAL_EARTHQUAKE_ASSET_KEY,
                version=version.version,
                parameters={
                    "event_id": str(event.id),
                    "historical_event_id": arguments["historical_event_id"],
                },
                limitations=("historical_event_not_found",),
            )
        properties = dict(row["properties"] or {})
        historical_event_id = str(
            properties.get("event_id") or row["business_key"]
        )
        return ToolResult.ok(
            value={
                "event_id": str(event.id),
                "historical_event_id": historical_event_id,
                "distance_km": _rounded_km(row["distance_km"]),
            },
            unit="km",
            source=HISTORICAL_EARTHQUAKE_ASSET_KEY,
            version=version.version,
            parameters={
                "event_id": str(event.id),
                "historical_event_id": arguments["historical_event_id"],
            },
        )


def register_seismicity_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="seismicity.within_radius",
            description="按当前事件震中查询半径内历史地震",
            input_model=SeismicityWithinRadiusInput,
            handler=SeismicityWithinRadiusTool().handle,
            timeout_seconds=1.5,
            parallel_safe=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="seismicity.distance",
            description="计算当前事件与锁定历史地震目录记录的距离",
            input_model=SeismicityDistanceInput,
            handler=SeismicityDistanceTool().handle,
            timeout_seconds=1.5,
            parallel_safe=False,
        )
    )


def _historical_event_payload(
    *,
    business_key: str,
    properties: dict[str, Any],
    distance_km: Decimal | float | int,
) -> dict[str, Any]:
    return {
        "event_id": str(properties.get("event_id") or business_key),
        "business_key": business_key,
        "origin_time": properties.get("origin_time"),
        "longitude": _optional_float(properties.get("longitude")),
        "latitude": _optional_float(properties.get("latitude")),
        "depth_km": _optional_float(properties.get("depth_km")),
        "magnitude": _optional_float(properties.get("magnitude")),
        "place": properties.get("place"),
        "source": properties.get("source"),
        "disaster_flag": properties.get("disaster_flag"),
        "distance_km": _rounded_km(distance_km),
    }


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _rounded_km(value: Decimal | float | int) -> float:
    return round(float(value), 3)
