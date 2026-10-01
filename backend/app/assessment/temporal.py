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
from temporalio.workflow import ParentClosePolicy

from app.artifacts.workflow import (
    DEFAULT_ARTIFACT_RENDER_CONCURRENCY,
    ArtifactProductionWorkflow,
    ArtifactProductionWorkflowInput,
    artifact_production_workflow_id,
)

if TYPE_CHECKING:
    from app.intensity.service import IntensityService
    from app.loss.service import LossAssessmentService

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
    task_deadlines: tuple[datetime, ...]
    production_run_id: str | None
    artifact_workflow_input: ArtifactProductionWorkflowInput | None


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
class AssessmentTimeoutInput:
    safety_timeout_seconds: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class AssessmentWorkflowResult:
    run_id: str
    task_count: int
    status: str
    production_run_id: str | None = None


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
        artifact_child_handle = None
        if prepared.artifact_workflow_input is not None:
            artifact_child_handle = await workflow.start_child_workflow(
                ArtifactProductionWorkflow.run,
                prepared.artifact_workflow_input,
                id=artifact_production_workflow_id(
                    prepared.production_run_id
                ),
                parent_close_policy=ParentClosePolicy.ABANDON,
            )
            await workflow.execute_activity(
                "mark_artifact_production_launched",
                AssessmentRunActivityInput(prepared.run_id),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_retry_policy(),
            )
        deadline_task = asyncio.create_task(self._mark_deadline_when_due(prepared))
        task_deadline_task = asyncio.create_task(
            self._observe_task_deadlines_when_due(prepared)
        )
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
                if artifact_child_handle is not None:
                    await self._signal_child(
                        artifact_child_handle,
                        ArtifactProductionWorkflow.assessment_failed,
                    )
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
                if artifact_child_handle is not None:
                    await self._signal_child(
                        artifact_child_handle,
                        ArtifactProductionWorkflow.intensity_ready,
                    )

                building_handle = workflow.start_activity(
                    "run_loss_buildings",
                    IntensityActivityInput(prepared.run_id, "loss.buildings"),
                    start_to_close_timeout=timedelta(seconds=1800),
                    retry_policy=_retry_policy(),
                )
                population_handle = workflow.start_activity(
                    "run_loss_population",
                    IntensityActivityInput(prepared.run_id, "loss.population"),
                    start_to_close_timeout=timedelta(seconds=1800),
                    retry_policy=_retry_policy(),
                )
                first_stage = await asyncio.gather(
                    building_handle,
                    population_handle,
                    return_exceptions=True,
                )
                if any(isinstance(value, BaseException) for value in first_stage):
                    outcome = "failed"

                if outcome == "completed":
                    if artifact_child_handle is not None:
                        await self._signal_child(
                            artifact_child_handle,
                            ArtifactProductionWorkflow.loss_core_ready,
                        )
                    casualty_handle = workflow.start_activity(
                        "run_loss_casualties",
                        IntensityActivityInput(prepared.run_id, "loss.casualties"),
                        start_to_close_timeout=timedelta(seconds=1800),
                        retry_policy=_retry_policy(),
                    )
                    economic_handle = workflow.start_activity(
                        "run_loss_economic",
                        IntensityActivityInput(prepared.run_id, "loss.economic"),
                        start_to_close_timeout=timedelta(seconds=1800),
                        retry_policy=_retry_policy(),
                    )
                    second_stage = await asyncio.gather(
                        casualty_handle,
                        economic_handle,
                        return_exceptions=True,
                    )
                    if any(
                        isinstance(value, BaseException)
                        for value in second_stage
                    ):
                        outcome = "failed"

                if outcome == "completed":
                    await workflow.execute_activity(
                        "run_loss_resources",
                        IntensityActivityInput(prepared.run_id, "loss.resources"),
                        start_to_close_timeout=timedelta(seconds=1800),
                        retry_policy=_retry_policy(),
                    )
                    await workflow.execute_activity(
                        "run_loss_validate",
                        IntensityActivityInput(prepared.run_id, "loss.validate"),
                        start_to_close_timeout=timedelta(seconds=1800),
                        retry_policy=_retry_policy(),
                    )
                    if artifact_child_handle is not None:
                        await self._signal_child(
                            artifact_child_handle,
                            ArtifactProductionWorkflow.loss_final_ready,
                        )
            except ActivityError:
                outcome = "failed"
            if artifact_child_handle is not None and outcome == "failed":
                await self._signal_child(
                    artifact_child_handle,
                    ArtifactProductionWorkflow.assessment_failed,
                )
            return await self._finalize(prepared, outcome=outcome)
        finally:
            if not deadline_task.done():
                deadline_task.cancel()
                await asyncio.gather(deadline_task, return_exceptions=True)
            if not task_deadline_task.done():
                task_deadline_task.cancel()
                await asyncio.gather(
                    task_deadline_task,
                    return_exceptions=True,
                )

    async def _signal_child(
        self,
        child_handle: object,
        signal_method: object,
    ) -> None:
        try:
            await child_handle.signal(signal_method)
        except Exception as exc:
            logger.warning(
                "artifact child signal failed: %s",
                exc,
            )

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

    async def _observe_task_deadlines_when_due(
        self,
        prepared: PreparedAssessment,
    ) -> None:
        current_time = workflow.now()
        for deadline in sorted(set(prepared.task_deadlines)):
            delay = deadline - current_time
            if delay.total_seconds() > 0:
                await workflow.sleep(delay)
            await workflow.execute_activity(
                "observe_task_deadlines",
                AssessmentRunActivityInput(prepared.run_id),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_retry_policy(),
            )
            current_time = max(current_time, deadline)

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
            production_run_id=prepared.production_run_id,
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
        task_deadlines=tuple(
            _as_datetime(item)
            for item in value.get("task_deadlines", [])
        ),
        production_run_id=(
            str(value["production_run_id"])
            if value.get("production_run_id") is not None
            else None
        ),
        artifact_workflow_input=_as_artifact_workflow_input(
            value.get("artifact_workflow_input")
        ),
    )


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError("prepared assessment task deadline must be a timestamp")


def _as_artifact_workflow_input(value: object) -> ArtifactProductionWorkflowInput | None:
    if value is None or isinstance(value, ArtifactProductionWorkflowInput):
        return value
    if not isinstance(value, dict):
        raise TypeError("artifact workflow input must be a mapping")
    deadline = value.get("deadline_at")
    if not isinstance(deadline, str):
        deadline = str(deadline)
    return ArtifactProductionWorkflowInput(
        production_run_id=str(value["production_run_id"]),
        assessment_run_id=str(value["assessment_run_id"]),
        event_id=str(value["event_id"]),
        revision_id=str(value["revision_id"]),
        deadline_at=deadline,
        catalog_version=str(value["catalog_version"]),
        context_fingerprint=str(value["context_fingerprint"]),
        launch_mode=str(value["launch_mode"]),
        generation_seq=int(value["generation_seq"]),
        generation_scope=str(value["generation_scope"]),
        required_outputs=tuple(
            (str(item[0]), str(item[1])) for item in value["required_outputs"]
        ),
        render_concurrency=int(
            value.get(
                "render_concurrency",
                DEFAULT_ARTIFACT_RENDER_CONCURRENCY,
            )
        ),
        deadline_basis_at=str(
            value.get("deadline_basis_at") or deadline
        ),
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
        loss_service_factory: Callable[[], LossAssessmentService] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._intensity_service_factory = (
            intensity_service_factory or self._default_intensity_service_factory
        )
        self._loss_service_factory = (
            loss_service_factory or self._default_loss_service_factory
        )

    def _default_intensity_service_factory(self) -> IntensityService:
        from app.config import settings
        from app.intensity.parameters import load_parameter_bundle
        from app.intensity.service import IntensityService

        return IntensityService(
            session_factory=self._session_factory,
            parameters=load_parameter_bundle(settings.intensity_parameters_path),
        )

    def _default_loss_service_factory(self) -> LossAssessmentService:
        from app.loss.service import LossAssessmentService

        return LossAssessmentService(session_factory=self._session_factory)

    @activity.defn(name="prepare_assessment")
    async def prepare_assessment(
        self,
        request: AssessmentWorkflowInput,
    ) -> PreparedAssessment:
        from app.config import settings
        from app.assessment.repository import AssessmentRepository
        from app.artifacts.catalog import load_catalog
        from app.artifacts.repository import (
            ArtifactProductionRepository,
            CreateProductionRunCommand,
        )
        from app.events.models import EarthquakeRevision

        repository = AssessmentRepository()
        production_run_id = None
        artifact_workflow_input = None
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
                task_deadlines = await repository.list_task_deadlines(
                    session,
                    run.id,
                )
                catalog = load_catalog(settings.artifact_catalog_path)
                revision = await session.get(EarthquakeRevision, run.revision_id)
                if revision is None:
                    raise LookupError("assessment revision not found")
                production_mode = {
                    "formal": "live",
                    "correction": "live",
                    "manual": "manual",
                    "test": "test",
                    "drill": "drill",
                    "replay": "replay",
                }.get(revision.revision_kind)
                if production_mode is None:
                    raise ValueError(
                        f"unsupported artifact production revision kind: "
                        f"{revision.revision_kind}"
                    )
                repository = ArtifactProductionRepository(catalog)
                production = await repository.create_run(
                    session,
                    CreateProductionRunCommand(
                        assessment_run_id=run.id,
                        event_id=run.event_id,
                        revision_id=run.revision_id,
                        revision_no=revision.revision_no,
                        production_mode=production_mode,
                        launch_mode="assessment_child",
                        deadline_basis_at=run.deadline_basis_at,
                        deadline_at=run.deadline_at,
                        deadline_kind="event_deadline",
                        catalog_version=catalog.catalog_version,
                        generation_scope="full",
                        required_outputs=catalog.full_required_outputs(),
                        snapshot={
                            "launch": "assessment_child",
                            "event_id": str(run.event_id),
                            "revision_id": str(run.revision_id),
                            "revision_no": revision.revision_no,
                            "assessment_run_id": str(run.id),
                        },
                        reuse_existing=True,
                    ),
                )
                tasks = await repository.list_tasks(session, production.id)
                production_run_id = str(production.id)
                artifact_workflow_input = ArtifactProductionWorkflowInput(
                    production_run_id=production_run_id,
                    assessment_run_id=str(run.id),
                    event_id=str(run.event_id),
                    revision_id=str(run.revision_id),
                    deadline_at=production.deadline_at.isoformat(),
                    catalog_version=catalog.catalog_version,
                    context_fingerprint="",
                    launch_mode="assessment_child",
                    generation_seq=1,
                    generation_scope="full",
                    required_outputs=tuple(
                        (task.artifact_key, task.output_profile)
                        for task in tasks
                    ),
                    render_concurrency=settings.artifact_render_concurrency,
                    deadline_basis_at=production.deadline_basis_at.isoformat(),
                )
        return PreparedAssessment(
            run_id=str(run.id),
            task_count=task_count,
            deadline_at=run.deadline_at,
            task_deadlines=tuple(task_deadlines),
            production_run_id=production_run_id,
            artifact_workflow_input=artifact_workflow_input,
        )

    @activity.defn(name="mark_artifact_production_launched")
    async def mark_artifact_production_launched(
        self,
        request: AssessmentRunActivityInput,
    ):
        from app.assessment.repository import AssessmentRepository

        repository = AssessmentRepository()
        async with self._session_factory() as session:
            async with session.begin():
                task = await repository.mark_artifact_production_launched(
                    session,
                    request.run_id,
                )
                if task is None:
                    return None
                return task.status

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

    @activity.defn(name="run_loss_buildings")
    async def run_loss_buildings(self, request: IntensityActivityInput):
        service = self._loss_service_factory()
        return await service.run_buildings(request.run_id)

    @activity.defn(name="run_loss_population")
    async def run_loss_population(self, request: IntensityActivityInput):
        service = self._loss_service_factory()
        return await service.run_population(request.run_id)

    @activity.defn(name="run_loss_casualties")
    async def run_loss_casualties(self, request: IntensityActivityInput):
        service = self._loss_service_factory()
        return await service.run_casualties(request.run_id)

    @activity.defn(name="run_loss_economic")
    async def run_loss_economic(self, request: IntensityActivityInput):
        service = self._loss_service_factory()
        return await service.run_economic(request.run_id)

    @activity.defn(name="run_loss_resources")
    async def run_loss_resources(self, request: IntensityActivityInput):
        service = self._loss_service_factory()
        return await service.run_resources(request.run_id)

    @activity.defn(name="run_loss_validate")
    async def run_loss_validate(self, request: IntensityActivityInput):
        service = self._loss_service_factory()
        return await service.run_validate(request.run_id)

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

    @activity.defn(name="observe_task_deadlines")
    async def observe_task_deadlines(
        self,
        request: AssessmentRunActivityInput,
    ):
        from app.assessment.repository import AssessmentRepository

        repository = AssessmentRepository()
        async with self._session_factory() as session:
            async with session.begin():
                warned = await repository.observe_task_deadlines(
                    session,
                    request.run_id,
                    datetime.now(UTC),
                )
        for task_key in warned:
            logger.warning(
                "assessment task deadline exceeded run_id=%s task_key=%s",
                request.run_id,
                task_key,
            )
        return warned

    @activity.defn(name="reconcile_assessment_timeouts")
    async def reconcile_assessment_timeouts(
        self,
        request: AssessmentTimeoutInput,
    ):
        from app.assessment.repository import AssessmentRepository

        repository = AssessmentRepository()
        async with self._session_factory() as session:
            async with session.begin():
                failed_ids = await repository.reconcile_timeouts(
                    session,
                    safety_timeout_seconds=request.safety_timeout_seconds,
                    observed_at=request.observed_at,
                )
        for run_id in failed_ids:
            logger.error(
                "assessment workflow safety timeout run_id=%s",
                run_id,
            )
        return failed_ids

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
                        algorithm_bundle_version="intensity-loss-v1",
                    )
                    return None
                if run.status not in {"completed", "failed"}:
                    await repository.fail_run(
                        session,
                        run.id,
                        "assessment intensity chain failed",
                    )
                return None
