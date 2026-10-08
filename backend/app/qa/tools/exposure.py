from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.assessment.models import AssessmentRun
from app.data_assets.snapshot_service import DataAssetSnapshotService
from app.qa.tools.event import load_context_event_and_revision
from app.qa.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)

ADMIN_TOWN_ASSET_KEY = "shanghai.admin.town"
POPULATION_TOWN_ASSET_KEY = "shanghai.population.town"

_POPULATION_SQL = text(
    """
    SELECT
      population.business_key,
      population.properties
    FROM data_asset_records AS population
    JOIN data_asset_records AS town
      ON town.version_id = :admin_version_id
     AND town.business_key = population.business_key
    WHERE population.version_id = :population_version_id
      AND town.geom IS NOT NULL
      AND ST_DWithin(
        town.geom::geography,
        ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)::geography,
        :radius_m
      )
      AND (
        CAST(:area_code AS VARCHAR) IS NULL
        OR population.business_key = CAST(:area_code AS VARCHAR)
        OR population.business_key LIKE CAST(:area_prefix AS VARCHAR)
        OR (
          CAST(:city_prefix AS VARCHAR) IS NOT NULL
          AND population.business_key LIKE CAST(:city_prefix AS VARCHAR)
        )
      )
    ORDER BY population.business_key
    """
)


class ExposurePopulationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID | None = None
    radius_km: Decimal = Field(default=Decimal("50"), gt=0, le=500)
    area_code: str | None = Field(default=None, max_length=64)


class ExposurePopulationTool:
    def __init__(
        self,
        snapshots: DataAssetSnapshotService | None = None,
    ) -> None:
        self._snapshots = snapshots or DataAssetSnapshotService()

    async def handle(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        requested_event_id = arguments.get("event_id")
        if (
            requested_event_id is not None
            and context.event_id is not None
            and str(requested_event_id) != str(context.event_id)
        ):
            return ToolResult.invalid(
                limitations=("event_context_mismatch",),
                parameters={"event_id": str(requested_event_id)},
            )
        event_id = context.event_id or requested_event_id
        if event_id is None:
            return ToolResult.not_found(limitations=("event_required",))
        if context.assessment_run_id is None:
            return ToolResult.unavailable(
                limitations=("assessment_run_missing",),
                parameters={"event_id": str(event_id)},
            )
        assessment_run = await context.session.get(
            AssessmentRun,
            context.assessment_run_id,
        )
        if assessment_run is None or assessment_run.event_id != UUID(str(event_id)):
            return ToolResult.unavailable(
                limitations=("assessment_run_missing",),
                parameters={"event_id": str(event_id)},
            )
        if assessment_run.revision_id != context.revision_id:
            return _assessment_revision_mismatch_result(
                context,
                assessment_run,
                event_id,
            )

        event, revision, error = await load_context_event_and_revision(
            context,
            event_id,
        )
        if error is not None:
            return error
        assert event is not None
        assert revision is not None

        population_version = await self._snapshots.get_locked_version(
            context.session,
            run_id=context.assessment_run_id,
            asset_key=POPULATION_TOWN_ASSET_KEY,
        )
        if population_version is None:
            return ToolResult.unavailable(
                limitations=("population_asset_missing",),
                source=POPULATION_TOWN_ASSET_KEY,
                parameters={"event_id": str(event.id)},
            )
        admin_version = await self._snapshots.get_locked_version(
            context.session,
            run_id=context.assessment_run_id,
            asset_key=ADMIN_TOWN_ASSET_KEY,
        )
        if admin_version is None:
            return ToolResult.unavailable(
                limitations=("population_geometry_asset_missing",),
                source=POPULATION_TOWN_ASSET_KEY,
                parameters={"event_id": str(event.id)},
            )

        radius_km = arguments.get("radius_km", Decimal("50"))
        area_code = arguments.get("area_code")
        rows = (
            await context.session.execute(
                _POPULATION_SQL,
                {
                    "population_version_id": population_version.id,
                    "admin_version_id": admin_version.id,
                    "longitude": revision.longitude,
                    "latitude": revision.latitude,
                    "radius_m": float(radius_km) * 1000.0,
                    "area_code": area_code,
                    "area_prefix": f"{area_code}%" if area_code else None,
                    "city_prefix": _city_prefix(area_code),
                },
            )
        ).mappings().all()

        regions: list[dict[str, Any]] = []
        for row in rows:
            properties = dict(row["properties"] or {})
            try:
                resident = _population_number(properties, "resident")
                floating = _population_number(properties, "floating")
                total = _population_number(properties, "total")
            except ValueError:
                return ToolResult.unavailable(
                    limitations=("population_value_missing_or_invalid",),
                    source=POPULATION_TOWN_ASSET_KEY,
                    version=population_version.version,
                    parameters={"event_id": str(event.id)},
                )
            precision = max(
                _decimal_precision(resident),
                _decimal_precision(floating),
                _decimal_precision(total),
            )
            regions.append(
                {
                    "area_code": row["business_key"],
                    "area_name": (
                        properties.get("NAME")
                        or properties.get("name")
                        or row["business_key"]
                    ),
                    "resident_population": _json_number(resident),
                    "floating_population": _json_number(floating),
                    "total_population": _json_number(total),
                    "precision": precision,
                    "unit": "人",
                    "quality_grade": population_version.quality_grade,
                    "value_status": population_version.status,
                }
            )

        if not regions:
            return ToolResult.not_found(
                source=POPULATION_TOWN_ASSET_KEY,
                version=population_version.version,
                parameters={
                    "event_id": str(event.id),
                    "radius_km": float(radius_km),
                    "area_code": area_code,
                },
                limitations=("population_area_not_found",),
            )

        return ToolResult.ok(
            value={
                "event_id": str(event.id),
                "run_id": str(assessment_run.id),
                "run_revision_id": str(assessment_run.revision_id),
                "radius_km": float(radius_km),
                "area_code": area_code,
                "resident_population": _json_number(
                    sum(_population_decimal(region, "resident_population") for region in regions)
                ),
                "floating_population": _json_number(
                    sum(_population_decimal(region, "floating_population") for region in regions)
                ),
                "total_population": _json_number(
                    sum(_population_decimal(region, "total_population") for region in regions)
                ),
                "regions": regions,
                "data_versions": {
                    ADMIN_TOWN_ASSET_KEY: admin_version.version,
                    POPULATION_TOWN_ASSET_KEY: population_version.version,
                },
                "checksums": {
                    ADMIN_TOWN_ASSET_KEY: admin_version.checksum,
                    POPULATION_TOWN_ASSET_KEY: population_version.checksum,
                },
                "statuses": {
                    ADMIN_TOWN_ASSET_KEY: admin_version.status,
                    POPULATION_TOWN_ASSET_KEY: population_version.status,
                },
                "quality_grades": {
                    ADMIN_TOWN_ASSET_KEY: admin_version.quality_grade,
                    POPULATION_TOWN_ASSET_KEY: population_version.quality_grade,
                },
                "precision": max(region["precision"] for region in regions),
                "unit": "人",
            },
            unit="人",
            source=POPULATION_TOWN_ASSET_KEY,
            version=population_version.version,
            parameters={
                "event_id": str(event.id),
                "run_id": str(assessment_run.id),
                "run_revision_id": str(assessment_run.revision_id),
                "radius_km": float(radius_km),
                "area_code": area_code,
            },
        )


def register_exposure_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="exposure.population",
            description="查询锁定评估运行内指定半径和行政区域的常住、流动和总人口",
            input_model=ExposurePopulationInput,
            handler=ExposurePopulationTool().handle,
            timeout_seconds=1.5,
            parallel_safe=False,
        )
    )


def _population_number(
    properties: dict[str, Any],
    field_name: str,
) -> Decimal:
    value = properties.get(field_name)
    if value is None or value == "":
        raise ValueError(field_name)
    try:
        number = Decimal(str(value))
    except (ValueError, TypeError) as exc:
        raise ValueError(field_name) from exc
    if not number.is_finite() or number < 0:
        raise ValueError(field_name)
    return number


def _population_decimal(region: dict[str, Any], field_name: str) -> Decimal:
    return Decimal(str(region[field_name]))


def _decimal_precision(value: Decimal) -> int:
    return max(0, -value.as_tuple().exponent)


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _city_prefix(area_code: str | None) -> str | None:
    if (
        area_code is not None
        and len(area_code) == 6
        and area_code.endswith("0000")
    ):
        return f"{area_code[:2]}%"
    return None


def _assessment_revision_mismatch_result(
    context: ToolContext,
    run: AssessmentRun,
    event_id: object,
) -> ToolResult:
    return ToolResult.unavailable(
        limitations=("assessment_revision_mismatch",),
        parameters={
            "event_id": str(event_id),
            "run_id": str(run.id),
            "run_revision_id": str(run.revision_id),
            "context_revision_id": (
                str(context.revision_id)
                if context.revision_id is not None
                else None
            ),
        },
    )
