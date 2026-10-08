from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.assessment.models import AssessmentRun
from app.assessment.repository import AssessmentRepository
from app.intensity.repository import IntensityRepository
from app.qa.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)

_PRODUCT_TYPES = ("model", "instrument", "fusion")


class IntensityGetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID | None = None


class IntensityGetTool:
    def __init__(
        self,
        assessment_repository: AssessmentRepository | None = None,
        intensity_repository: IntensityRepository | None = None,
    ) -> None:
        self._assessment_repository = assessment_repository or AssessmentRepository()
        self._intensity_repository = intensity_repository or IntensityRepository()

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

        run = await _resolve_run(
            context,
            event_id,
            self._assessment_repository,
        )
        if run is None:
            return ToolResult.unavailable(
                limitations=("assessment_run_missing",),
                parameters={"event_id": str(event_id)},
            )

        products = await self._intensity_repository.list_products(
            context.session,
            run.id,
        )
        if not products:
            return ToolResult.unavailable(
                limitations=("intensity_products_missing",),
                parameters={"event_id": str(event_id), "run_id": str(run.id)},
            )
        by_type = {
            str(product["product_type"]): product for product in products
        }
        return ToolResult.ok(
            value={
                "event_id": str(event_id),
                "run_id": str(run.id),
                **{
                    product_type: _product_payload(
                        by_type.get(product_type)
                    )
                    for product_type in _PRODUCT_TYPES
                },
            },
            source="intensity_field_products",
            version=str(run.id),
            parameters={"event_id": str(event_id), "run_id": str(run.id)},
        )


def register_intensity_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="intensity.get",
            description="查询锁定评估运行的模型、仪器和融合烈度产品状态",
            input_model=IntensityGetInput,
            handler=IntensityGetTool().handle,
            timeout_seconds=1.5,
            parallel_safe=False,
        )
    )


async def _resolve_run(
    context: ToolContext,
    event_id: UUID,
    repository: AssessmentRepository,
) -> AssessmentRun | None:
    if context.assessment_run_id is not None:
        run = await context.session.get(AssessmentRun, context.assessment_run_id)
        if run is None or run.event_id != event_id:
            return None
        return run
    return await repository.get_effective_run(
        context.session,
        event_id=str(event_id),
    )


def _product_payload(product: dict[str, Any] | None) -> dict[str, Any]:
    if product is None:
        return {
            "product_id": None,
            "status": "unavailable",
            "quality_grade": None,
            "coverage_ratio": None,
            "algorithm_version": None,
            "parameter_version": None,
            "strategy_version": None,
            "grid_definition_version": None,
            "region_profile_version": None,
            "statistics": {},
            "checksum": None,
        }
    return {
        "product_id": product.get("product_id"),
        "status": product.get("status"),
        "quality_grade": product.get("quality_grade"),
        "coverage_ratio": product.get("coverage_ratio"),
        "algorithm_version": product.get("algorithm_version"),
        "parameter_version": product.get("parameter_version"),
        "strategy_version": product.get("strategy_version"),
        "grid_definition_version": product.get("grid_definition_version"),
        "region_profile_version": product.get("region_profile_version"),
        "statistics": dict(product.get("statistics") or {}),
        "checksum": product.get("output_checksum"),
    }
