from __future__ import annotations

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

_REGION_ASSETS = (
    ("town", "shanghai.admin.town"),
    ("county", "shanghai.admin.county"),
    ("city", "shanghai.admin.city"),
)

_REGION_LOOKUP_SQL = text(
    """
    SELECT
      business_key,
      properties
    FROM data_asset_records
    WHERE version_id = :version_id
      AND geom IS NOT NULL
      AND ST_Covers(
        geom,
        ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)
      )
    ORDER BY business_key
    """
)


class RegionLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID | None = None


class RegionLookupTool:
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

        regions: list[dict[str, Any]] = []
        versions: dict[str, str] = {}
        missing: list[str] = []
        for level, asset_key in _REGION_ASSETS:
            version = await resolve_locked_asset_version(context, asset_key)
            if version is None:
                missing.append(asset_key)
                continue
            versions[asset_key] = version.version
            rows = (
                await context.session.execute(
                    _REGION_LOOKUP_SQL,
                    {
                        "version_id": version.id,
                        "longitude": revision.longitude,
                        "latitude": revision.latitude,
                    },
                )
            ).mappings().all()
            for row in rows:
                properties = dict(row["properties"] or {})
                regions.append(
                    {
                        "level": level,
                        "asset_key": asset_key,
                        "business_key": row["business_key"],
                        "name": (
                            properties.get("NAME")
                            or properties.get("name")
                            or row["business_key"]
                        ),
                        "properties": properties,
                    }
                )

        if not regions:
            return ToolResult.not_found(
                source="shanghai.admin",
                version=_version_label(versions),
                parameters={"event_id": str(event.id)},
                limitations=tuple(
                    [*(f"missing:{asset_key}" for asset_key in missing), "region_not_found"]
                ),
            )

        smallest_level = regions[0]["level"]
        limitations = tuple(
            f"missing:{asset_key}" for asset_key in sorted(missing)
        )
        return ToolResult.ok(
            value={
                "event_id": str(event.id),
                "longitude": float(revision.longitude),
                "latitude": float(revision.latitude),
                "smallest_level": smallest_level,
                "level": smallest_level,
                "regions": regions,
                "versions": versions,
            },
            source="shanghai.admin",
            version=_version_label(versions),
            parameters={"event_id": str(event.id)},
            limitations=limitations,
        )


def register_region_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="region.lookup",
            description="按当前事件震中定位街镇、区县和市域行政边界",
            input_model=RegionLookupInput,
            handler=RegionLookupTool().handle,
            timeout_seconds=1.5,
            parallel_safe=False,
        )
    )


def _version_label(versions: dict[str, str]) -> str:
    return ",".join(
        f"{asset_key}={version}" for asset_key, version in sorted(versions.items())
    )
