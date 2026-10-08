from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.assessment.models import AssessmentRun
from app.assessment.repository import AssessmentRepository
from app.loss.models import LossMetricValue, LossProduct
from app.loss.repository import LossRepository
from app.qa.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)


class LossMetricsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID | None = None
    product_type: Literal[
        "building_damage",
        "population_impact",
        "casualties",
        "economic_loss",
        "resource_demand",
        "validation",
    ]
    area_scope: Literal["city", "county", "town"] = "city"
    area_code: str | None = Field(default=None, max_length=64)
    metric_keys: list[str] = Field(default_factory=list, max_length=50)
    value_type: Literal["low", "central", "high"] = "central"

    @field_validator("area_code")
    @classmethod
    def validate_area_code(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("area_code must not be empty")
        return value.strip() if value is not None else None


class LossMetricsTool:
    def __init__(
        self,
        assessment_repository: AssessmentRepository | None = None,
        loss_repository: LossRepository | None = None,
    ) -> None:
        self._assessment_repository = assessment_repository or AssessmentRepository()
        self._loss_repository = loss_repository or LossRepository()

    async def handle(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        if (
            arguments.get("area_code") is not None
            and not str(arguments["area_code"]).strip()
        ):
            return ToolResult.invalid(
                limitations=("area_code_required",),
                parameters={"area_code": arguments["area_code"]},
            )
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
        if run.revision_id != context.revision_id:
            return _assessment_revision_mismatch_result(
                context,
                run,
                event_id,
            )
        products = await self._loss_repository.list_products(
            context.session,
            run.id,
        )
        product = next(
            (
                item
                for item in products
                if item.product_type == arguments["product_type"]
            ),
            None,
        )
        if product is None:
            return ToolResult.unavailable(
                limitations=("loss_product_missing",),
                parameters={
                    "event_id": str(event_id),
                    "run_id": str(run.id),
                    "product_type": arguments["product_type"],
                },
            )

        statement = select(LossMetricValue).where(
            LossMetricValue.product_id == product.id,
            LossMetricValue.area_scope == arguments["area_scope"],
            LossMetricValue.value_type == arguments["value_type"],
        )
        if arguments.get("area_code") is not None:
            statement = statement.where(
                LossMetricValue.area_code == arguments["area_code"]
            )
        metric_keys = arguments.get("metric_keys") or []
        if metric_keys:
            statement = statement.where(
                LossMetricValue.metric_key.in_(metric_keys)
            )
        metrics = (
            await context.session.scalars(
                statement.order_by(
                    LossMetricValue.area_code,
                    LossMetricValue.metric_key,
                )
            )
        ).all()
        return ToolResult.ok(
            value={
                "event_id": str(event_id),
                "run_id": str(run.id),
                "run_revision_id": str(run.revision_id),
                **_product_metadata(product),
                "metrics": [_metric_payload(metric) for metric in metrics],
            },
            source="loss_products",
            version=product.output_checksum,
            limitations=() if metrics else ("no_metric_values",),
            parameters={
                "event_id": str(event_id),
                "run_id": str(run.id),
                "run_revision_id": str(run.revision_id),
                "product_type": product.product_type,
                "area_scope": arguments["area_scope"],
                "area_code": arguments.get("area_code"),
                "metric_keys": metric_keys,
                "value_type": arguments["value_type"],
            },
        )


def register_loss_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="loss.get_metrics",
            description="查询锁定评估运行的损失产品指标、版本和值状态",
            input_model=LossMetricsInput,
            handler=LossMetricsTool().handle,
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


def _metric_payload(metric: LossMetricValue) -> dict[str, Any]:
    return {
        "area_scope": metric.area_scope,
        "area_code": metric.area_code,
        "area_name": metric.area_name,
        "metric_key": metric.metric_key,
        "value_type": metric.value_type,
        "numeric_value": (
            float(metric.numeric_value)
            if metric.numeric_value is not None
            else None
        ),
        "unit": metric.unit,
        "precision": metric.precision,
        "quality_grade": metric.quality_grade,
        "value_status": metric.value_status,
        "note": metric.note,
    }


def _product_metadata(product: LossProduct) -> dict[str, Any]:
    return {
        "product_id": str(product.id),
        "task_id": str(product.task_id),
        "product_type": product.product_type,
        "status": product.status,
        "quality_grade": product.quality_grade,
        "calibration_status": product.calibration_status,
        "coverage_ratio": float(product.coverage_ratio),
        "partial_scope": product.partial_scope,
        "needs_review": product.needs_review,
        "spatialized_estimate": product.spatialized_estimate,
        "algorithm_version": product.algorithm_version,
        "parameter_version": product.parameter_version,
        "region_profile_version": product.region_profile_version,
        "input_fingerprint": product.input_fingerprint,
        "input_checksum": product.input_checksum,
        "output_checksum": product.output_checksum,
        "checksum": product.output_checksum,
        "statistics": dict(product.statistics or {}),
        "reason": product.reason,
        "created_at": _isoformat(product.created_at),
        "completed_at": _isoformat(product.completed_at),
        "published_at": _isoformat(product.published_at),
    }


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


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
