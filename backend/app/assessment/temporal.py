from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

if TYPE_CHECKING:
    from app.intensity.service import IntensityService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AssessmentWorkflowInput:
    event_id: str
    revision_id: str
    outbox_id: str


@dataclass(frozen=True, slots=True)
class PreparedAssessment:
    run_id: str
    task_count: int
    deadline_at: datetime


@dataclass(frozen=True, slots=True)
class IntensityActivityInput:
    run_id: str
    task_key: str


@dataclass(frozen=True, slots=True)
class AssessmentRunActivityInput:
    run_id: str


@dataclass(frozen=True, slots=True)
class FinalizeAssessmentInput:
    run_id: str
    outcome: str


@dataclass(frozen=True, slots=True)
class AssessmentWorkflowResult:
    run_id: str
    task_count: int
    status: str


@workflow.defn
class AssessmentWorkflow:
    @workflow.run
    async def run(
        self,
        request: AssessmentWorkflowInput,
    ) -> AssessmentWorkflowResult:
        prepared_payload = await workflow.execute_activity(
            "prepare_assessment",
            request,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_retry_policy(),
        )
        prepared = _as_prepared_assessment(prepared_payload)
        deadline_task = asyncio.create_task(self._mark_deadline_when_due(prepared))
        try:
            model_handle = workflow.start_activity(
                "run_intensity_model",
                IntensityActivityInput(prepared.run_id, "intensity.model"),
                start_to_close_timeout=timedelta(seconds=1800),
                retry_policy=_retry_policy(),
            )
            instrument_handle = workflow.start_activity(
                "run_intensity_instrument",
                IntensityActivityInput(prepared.run_id, "intensity.instrument"),
                start_to_close_timeout=timedelta(seconds=1800),
                retry_policy=_retry_policy(),
            )
            model_result, _instrument_result = await asyncio.gather(
                model_handle,
                instrument_handle,
                return_exceptions=True,
            )

            if isinstance(model_result, BaseException):
                return await self._finalize(
                    prepared,
                    outcome="failed",
                )

            outcome = "completed"
            try:
                await workflow.execute_activity(
                    "run_intensity_fusion",
                    IntensityActivityInput(prepared.run_id, "intensity.fusion"),
                    start_to_close_timeout=timedelta(seconds=1800),
                    retry_policy=_retry_policy(),
                )
            except ActivityError:
                outcome = "failed"
            return await self._finalize(prepared, outcome=outcome)
        finally:
            if not deadline_task.done():
                deadline_task.cancel()
                await asyncio.gather(deadline_task, return_exceptions=True)

    async def _mark_deadline_when_due(
        self,
        prepared: PreparedAssessment,
    ) -> None:
        delay = prepared.deadline_at - workflow.now()
        if delay.total_seconds() > 0:
            await workflow.sleep(delay)
        await workflow.execute_activity(
            "mark_deadline_exceeded",
            AssessmentRunActivityInput(prepared.run_id),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_retry_policy(),
        )

    async def _finalize(
        self,
        prepared: PreparedAssessment,
        *,
        outcome: str,
    ) -> AssessmentWorkflowResult:
        await workflow.execute_activity(
            "finalize_assessment",
            FinalizeAssessmentInput(prepared.run_id, outcome),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_retry_policy(),
        )
        return AssessmentWorkflowResult(
            run_id=prepared.run_id,
            task_count=prepared.task_count,
            status=outcome,
        )


def _as_prepared_assessment(value: object) -> PreparedAssessment:
    if isinstance(value, PreparedAssessment):
        return value
    if not isinstance(value, dict):
        raise TypeError("prepare_assessment result must be a mapping")
    deadline = value.get("deadline_at")
    if isinstance(deadline, str):
        deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    if not isinstance(deadline, datetime):
        raise TypeError("prepare_assessment deadline_at must be a timestamp")
    return PreparedAssessment(
        run_id=str(value["run_id"]),
        task_count=int(value["task_count"]),
        deadline_at=deadline,
    )


def _retry_policy() -> RetryPolicy:
    return RetryPolicy(
        maximum_attempts=3,
        initial_interval=timedelta(seconds=2),
        maximum_interval=timedelta(seconds=8),
        backoff_coefficient=4.0,
    )


class AssessmentActivities:
    def __init__(
        self,
        session_factory: object,
        *,
        intensity_service_factory: Callable[[], IntensityService] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._intensity_service_factory = (
            intensity_service_factory or self._default_intensity_service_factory
        )

    def _default_intensity_service_factory(self) -> IntensityService:
        from app.config import settings
        from app.intensity.parameters import load_parameter_bundle
        from app.intensity.service import IntensityService

        return IntensityService(
            session_factory=self._session_factory,
            parameters=load_parameter_bundle(settings.intensity_parameters_path),
        )

    @activity.defn(name="prepare_assessment")
    async def prepare_assessment(
        self,
        request: AssessmentWorkflowInput,
    ) -> PreparedAssessment:
        from app.assessment.repository import AssessmentRepository

        repository = AssessmentRepository()
        async with self._session_factory() as session:
            async with session.begin():
                run = await repository.ensure_run_from_outbox(
                    session,
                    event_id=request.event_id,
                    revision_id=request.revision_id,
                    outbox_id=request.outbox_id,
                )
                await repository.start_run(session, run.id)
                task_count = await repository.count_tasks(session, run.id)
        return PreparedAssessment(
            run_id=str(run.id),
            task_count=task_count,
            deadline_at=run.deadline_at,
        )

    @activity.defn(name="run_intensity_model")
    async def run_intensity_model(self, request: IntensityActivityInput):
        service = self._intensity_service_factory()
        try:
            return await service.run_model(request.run_id)
        except (LookupError, ValueError, ArithmeticError) as exc:
            raise ApplicationError(
                f"{type(exc).__name__}: {exc}"[:2000],
                type=type(exc).__name__,
                non_retryable=True,
            ) from exc

    @activity.defn(name="run_intensity_instrument")
    async def run_intensity_instrument(self, request: IntensityActivityInput):
        service = self._intensity_service_factory()
        return await service.run_instrument(request.run_id)

    @activity.defn(name="run_intensity_fusion")
    async def run_intensity_fusion(self, request: IntensityActivityInput):
        service = self._intensity_service_factory()
        try:
            return await service.run_fusion(request.run_id)
        except (LookupError, ValueError, ArithmeticError) as exc:
            raise ApplicationError(
                f"{type(exc).__name__}: {exc}"[:2000],
                type=type(exc).__name__,
                non_retryable=True,
            ) from exc

    @activity.defn(name="mark_deadline_exceeded")
    async def mark_deadline_exceeded(self, request: AssessmentRunActivityInput):
        from app.assessment.repository import AssessmentRepository

        repository = AssessmentRepository()
        async with self._session_factory() as session:
            async with session.begin():
                marked = await repository.mark_deadline_exceeded(
                    session,
                    request.run_id,
                    datetime.now(UTC),
                )
        if marked:
            logger.warning(
                "assessment deadline exceeded run_id=%s",
                request.run_id,
            )
        return marked

    @activity.defn(name="finalize_assessment")
    async def finalize_assessment(self, request: FinalizeAssessmentInput):
        from app.assessment.repository import AssessmentRepository

        repository = AssessmentRepository()
        async with self._session_factory() as session:
            async with session.begin():
                run = await repository.get_run(session, request.run_id)
                if run is None:
                    raise LookupError("assessment run not found")
                await repository.mark_deadline_exceeded(
                    session,
                    run.id,
                    datetime.now(UTC),
                )
                if request.outcome == "completed":
                    await repository.skip_deferred_tasks(session, run.id)
                    await repository.complete_run(
                        session,
                        run.id,
                        algorithm_bundle_version="intensity-v1",
                    )
                    return None
                if run.status not in {"completed", "failed"}:
                    await repository.fail_run(
                        session,
                        run.id,
                        "assessment intensity chain failed",
                    )
                return None
