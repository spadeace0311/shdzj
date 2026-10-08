from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from app.qa.tools.event import load_context_event_and_revision
from app.qa.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    resolve_locked_asset_version,
)

FAULT_ASSET_KEY = "shanghai.fault"

_NEAREST_FAULT_SQL = text(
    """
    SELECT
      business_key,
      properties,
      ST_Distance(
        ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)::geography,
        geom::geography
      ) / 1000.0 AS distance_km
    FROM data_asset_records
    WHERE version_id = :version_id
      AND geom IS NOT NULL
    ORDER BY distance_km, business_key
    LIMIT 1
    """
)


class FaultNearestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID | None = None


class FaultNearestTool:
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
        version = await resolve_locked_asset_version(context, FAULT_ASSET_KEY)
        if version is None:
            return ToolResult.unavailable(
                limitations=("fault_asset_missing",),
                source=FAULT_ASSET_KEY,
                parameters={"event_id": str(event.id)},
            )
        row = (
            await context.session.execute(
                _NEAREST_FAULT_SQL,
                {
                    "version_id": version.id,
                    "longitude": revision.longitude,
                    "latitude": revision.latitude,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            return ToolResult.not_found(
                source=FAULT_ASSET_KEY,
                version=version.version,
                parameters={"event_id": str(event.id)},
                limitations=("fault_not_found",),
            )
        properties = dict(row["properties"] or {})
        distance_km = _rounded_km(row["distance_km"])
        return ToolResult.ok(
            value={
                "event_id": str(event.id),
                "fault_key": row["business_key"],
                "business_key": row["business_key"],
                "name": properties.get("name") or properties.get("NAME"),
                "distance_km": distance_km,
                "properties": properties,
            },
            unit="km",
            source=FAULT_ASSET_KEY,
            version=version.version,
            parameters={"event_id": str(event.id)},
        )


def register_fault_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="fault.nearest",
            description="按当前事件震中查询最近活动断层及距离",
            input_model=FaultNearestInput,
            handler=FaultNearestTool().handle,
            timeout_seconds=1.5,
            parallel_safe=False,
        )
    )


def _rounded_km(value: Decimal | float | int) -> float:
    return round(float(value), 3)
