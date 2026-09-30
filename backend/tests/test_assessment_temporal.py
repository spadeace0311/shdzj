from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.assessment.temporal import (
    AssessmentActivities,
    AssessmentRunActivityInput,
    AssessmentWorkflow,
    AssessmentWorkflowInput,
    FinalizeAssessmentInput,
)
from app.artifacts.worker import ArtifactActivities
from app.artifacts.workflow import ArtifactProductionWorkflow
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.loss.service import LossTaskOutcome
from app.regions.domain import RegionContext

_TEST_SERVER = os.environ.get("TEMPORAL_TEST_SERVER_EXECUTABLE")


def _artifact_activities(session_factory):
    artifacts = ArtifactActivities(session_factory)
    return [
        artifacts.prepare_artifact_production,
        artifacts.wait_for_artifact_dependencies,
        artifacts.render_map_artifact,
        artifacts.compose_docx_artifact,
        artifacts.compose_pptx_artifact,
        artifacts.validate_artifact_production,
        artifacts.publish_artifact_production,
        artifacts.terminalize_artifact_production,
        artifacts.cancel_artifact_production,
        artifacts.mark_production_deadline_exceeded,
    ]


@pytest.fixture(autouse=True)
async def clean_temporal_data(session_factory):
    await engine.dispose()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
    yield
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
    await engine.dispose()


async def _create_workflow_input(session_factory) -> AssessmentWorkflowInput:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="CENC-TEMPORAL-1",
        origin_time=datetime(2026, 9, 26, 3, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="上海测试位置",
        report_time=datetime(2026, 9, 26, 3, 2, tzinfo=UTC),
    )
    received_at = datetime(2026, 9, 26, 3, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "CENC-TEMPORAL-1", "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received_at,
        response_input=ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            deaths=None,
            max_intensity=None,
        ),
        region_context=RegionContext(
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            boundary_version="test-2026.1",
            computed_at=received_at,
        ),
    )
    async with session_factory() as session:
        outbox = await session.scalar(
            select(EventLifecycleOutbox).where(
                EventLifecycleOutbox.revision_id == outcome.revision_id
            )
        )
    assert outbox is not None
    return AssessmentWorkflowInput(
        event_id=outcome.event_id,
        revision_id=outcome.revision_id,
        outbox_id=str(outbox.id),
    )


def _test_intensity_service(session_factory):
    from app.intensity.domain import GridDefinition
    from app.intensity.parameters import load_parameter_bundle
    from app.intensity.service import IntensityService

    return IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "grid-temporal-test",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
    )


def _test_loss_service(session_factory):
    return _TestLossService(session_factory)


class _TestLossService:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def _complete(
        self,
        run_id: str,
        task_key: str,
    ) -> LossTaskOutcome:
        repository = AssessmentRepository()
        async with self._session_factory() as session:
            async with session.begin():
                task = await session.scalar(
                    select(AssessmentTask).where(
                        AssessmentTask.run_id == UUID(run_id),
                        AssessmentTask.task_key == task_key,
                    )
                )
                assert task is not None
                await repository.start_task(
                    session,
                    UUID(run_id),
                    task_key,
                    "loss-test-v1",
                    "f" * 64,
                )
                await repository.complete_task(
                    session,
                    task.id,
                    "c" * 64,
                    {"product_id": str(uuid4()), "task_key": task_key},
                )
        return _loss_outcome(run_id, task_key)

    async def run_buildings(self, run_id: str):
        return await self._complete(run_id, "loss.buildings")

    async def run_population(self, run_id: str):
        return await self._complete(run_id, "loss.population")

    async def run_casualties(self, run_id: str):
        return await self._complete(run_id, "loss.casualties")

    async def run_economic(self, run_id: str):
        return await self._complete(run_id, "loss.economic")

    async def run_resources(self, run_id: str):
        return await self._complete(run_id, "loss.resources")

    async def run_validate(self, run_id: str):
        return await self._complete(run_id, "loss.validate")


class _FailingFinalizeActivity:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @activity.defn(name="finalize_assessment")
    async def finalize_assessment(self, request: FinalizeAssessmentInput):
        self.calls.append(request.outcome)
        raise ApplicationError(
            "finalize failed",
            type="FinalizeFailure",
            non_retryable=True,
        )


class _DegradedInstrumentService:
    def __init__(self, delegate) -> None:
        self._delegate = delegate

    async def run_model(self, run_id: str):
        return await self._delegate.run_model(run_id)

    async def run_instrument(self, run_id: str):
        raise RuntimeError("instrument unavailable")

    async def run_fusion(self, run_id: str):
        return await self._delegate.run_fusion(run_id)


def _loss_outcome(run_id: str, task_key: str) -> LossTaskOutcome:
    now = datetime(2026, 9, 28, 2, 0, tzinfo=UTC)
    return LossTaskOutcome(
        product_id=uuid4(),
        task_key=task_key,
        status="succeeded",
        started_at=now,
        completed_at=now,
        stage_seconds={},
    )


async def _execute_loss_workflow(session_factory, loss_service):
    request = await _create_workflow_input(session_factory)
    activities = AssessmentActivities(
        session_factory,
        intensity_service_factory=lambda: _test_intensity_service(session_factory),
        loss_service_factory=lambda: loss_service,
    )
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=_TEST_SERVER,
    ) as environment:
        async with Worker(
            environment.client,
            task_queue="assessment-loss-failure-test",
            workflows=[AssessmentWorkflow, ArtifactProductionWorkflow],
            activities=[
                activities.prepare_assessment,
                activities.run_intensity_model,
                activities.run_intensity_instrument,
                activities.run_intensity_fusion,
                activities.run_loss_buildings,
                activities.run_loss_population,
                activities.run_loss_casualties,
                activities.run_loss_economic,
                activities.run_loss_resources,
                activities.run_loss_validate,
                activities.mark_deadline_exceeded,
                activities.observe_task_deadlines,
                activities.finalize_assessment,
                activities.mark_artifact_production_launched,
                *_artifact_activities(session_factory),
            ],
        ):
            return await environment.client.execute_workflow(
                AssessmentWorkflow.run,
                request,
                id=f"assessment-loss-failure:{request.event_id}",
                task_queue="assessment-loss-failure-test",
            )


async def test_assessment_workflow_prepares_run_and_tasks(session_factory) -> None:
    request = await _create_workflow_input(session_factory)
    activities = AssessmentActivities(
        session_factory,
        intensity_service_factory=lambda: _test_intensity_service(session_factory),
        loss_service_factory=lambda: _test_loss_service(session_factory),
    )

    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=_TEST_SERVER,
    ) as environment:
        async with Worker(
            environment.client,
            task_queue="assessment-test",
            workflows=[AssessmentWorkflow, ArtifactProductionWorkflow],
            activities=[
                activities.prepare_assessment,
                activities.run_intensity_model,
                activities.run_intensity_instrument,
                activities.run_intensity_fusion,
                activities.run_loss_buildings,
                activities.run_loss_population,
                activities.run_loss_casualties,
                activities.run_loss_economic,
                activities.run_loss_resources,
                activities.run_loss_validate,
                activities.mark_deadline_exceeded,
                activities.observe_task_deadlines,
                activities.finalize_assessment,
                activities.mark_artifact_production_launched,
                *_artifact_activities(session_factory),
            ],
        ):
            result = await environment.client.execute_workflow(
                AssessmentWorkflow.run,
                request,
                id=f"assessment:{request.event_id}:{request.revision_id}",
                task_queue="assessment-test",
            )

    async with session_factory() as session:
        run = await session.get(AssessmentRun, result.run_id)
        task_count = await session.scalar(
            select(func.count())
            .select_from(AssessmentTask)
            .where(AssessmentTask.run_id == result.run_id)
        )

    assert result.task_count == 11
    assert run is not None
    assert task_count == 11
    assert run.status == "completed"
    assert run.completed_at is not None


async def test_assessment_workflow_executes_loss_tasks_and_skips_deferred(
    session_factory,
) -> None:
    request = await _create_workflow_input(session_factory)
    activities = AssessmentActivities(
        session_factory,
        intensity_service_factory=lambda: _test_intensity_service(session_factory),
        loss_service_factory=lambda: _test_loss_service(session_factory),
    )

    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=_TEST_SERVER,
    ) as environment:
        async with Worker(
            environment.client,
            task_queue="assessment-intensity-test",
            workflows=[AssessmentWorkflow, ArtifactProductionWorkflow],
            activities=[
                activities.prepare_assessment,
                activities.run_intensity_model,
                activities.run_intensity_instrument,
                activities.run_intensity_fusion,
                activities.run_loss_buildings,
                activities.run_loss_population,
                activities.run_loss_casualties,
                activities.run_loss_economic,
                activities.run_loss_resources,
                activities.run_loss_validate,
                activities.mark_deadline_exceeded,
                activities.observe_task_deadlines,
                activities.finalize_assessment,
                activities.mark_artifact_production_launched,
                *_artifact_activities(session_factory),
            ],
        ):
            result = await environment.client.execute_workflow(
                AssessmentWorkflow.run,
                request,
                id=f"assessment-intensity:{request.event_id}",
                task_queue="assessment-intensity-test",
            )

    async with session_factory() as session:
        run = await session.get(AssessmentRun, result.run_id)
        tasks = (
            await session.scalars(
                select(AssessmentTask)
                .where(AssessmentTask.run_id == result.run_id)
                .order_by(AssessmentTask.sequence)
            )
        ).all()
        event = await session.get(EarthquakeEvent, request.event_id)

    assert run.status == "completed"
    assert run.completed_at is not None
    assert event.effective_assessment_run_id == run.id
    statuses = {task.task_key: task.status for task in tasks}
    assert statuses["intensity.model"] == "succeeded"
    assert statuses["intensity.instrument"] == "succeeded"
    assert statuses["intensity.fusion"] == "succeeded"
    assert statuses["loss.buildings"] == "succeeded"
    assert statuses["loss.population"] == "succeeded"
    assert statuses["loss.casualties"] == "succeeded"
    assert statuses["loss.economic"] == "succeeded"
    assert statuses["loss.resources"] == "succeeded"
    assert statuses["loss.validate"] == "succeeded"
    assert statuses["artifact.production"] == "succeeded"
    assert statuses["workgroup.response_tasks"] == "skipped"


async def test_finalize_completed_is_not_reclassified_after_finalize_failure(
    session_factory,
) -> None:
    request = await _create_workflow_input(session_factory)
    activities = AssessmentActivities(
        session_factory,
        intensity_service_factory=lambda: _test_intensity_service(session_factory),
        loss_service_factory=lambda: _test_loss_service(session_factory),
    )
    failing_finalize = _FailingFinalizeActivity()

    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=_TEST_SERVER,
    ) as environment:
        async with Worker(
            environment.client,
            task_queue="assessment-finalize-failure-test",
            workflows=[AssessmentWorkflow, ArtifactProductionWorkflow],
            activities=[
                activities.prepare_assessment,
                activities.run_intensity_model,
                activities.run_intensity_instrument,
                activities.run_intensity_fusion,
                activities.run_loss_buildings,
                activities.run_loss_population,
                activities.run_loss_casualties,
                activities.run_loss_economic,
                activities.run_loss_resources,
                activities.run_loss_validate,
                activities.mark_deadline_exceeded,
                activities.observe_task_deadlines,
                failing_finalize.finalize_assessment,
                activities.mark_artifact_production_launched,
                *_artifact_activities(session_factory),
            ],
        ):
            with pytest.raises(Exception):
                await environment.client.execute_workflow(
                    AssessmentWorkflow.run,
                    request,
                    id=f"assessment-finalize-failure:{request.event_id}",
                    task_queue="assessment-finalize-failure-test",
                )

    assert failing_finalize.calls == ["completed"]


async def test_finalize_failed_is_idempotent_for_already_failed_run(
    session_factory,
) -> None:
    request = await _create_workflow_input(session_factory)
    activities = AssessmentActivities(session_factory)
    prepared = await activities.prepare_assessment(request)

    async with session_factory() as session:
        async with session.begin():
            run = await session.get(AssessmentRun, prepared.run_id)
            await AssessmentRepository().fail_run(
                session,
                run.id,
                "already failed",
            )

    await activities.finalize_assessment(
        FinalizeAssessmentInput(prepared.run_id, "failed")
    )

    async with session_factory() as session:
        run = await session.get(AssessmentRun, prepared.run_id)
    assert run.status == "failed"


async def test_deadline_marker_persists_without_canceling_workflow(
    session_factory,
) -> None:
    request = await _create_workflow_input(session_factory)
    async with session_factory() as session:
        async with session.begin():
            repository = AssessmentRepository()
            run = await repository.ensure_run_from_outbox(
                session,
                event_id=request.event_id,
                revision_id=request.revision_id,
                outbox_id=request.outbox_id,
            )
            run.deadline_at = datetime.now(UTC) - timedelta(seconds=1)

    activities = AssessmentActivities(
        session_factory,
        intensity_service_factory=lambda: _test_intensity_service(session_factory),
        loss_service_factory=lambda: _test_loss_service(session_factory),
    )

    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=_TEST_SERVER,
    ) as environment:
        async with Worker(
            environment.client,
            task_queue="assessment-deadline-test",
            workflows=[AssessmentWorkflow, ArtifactProductionWorkflow],
            activities=[
                activities.prepare_assessment,
                activities.run_intensity_model,
                activities.run_intensity_instrument,
                activities.run_intensity_fusion,
                activities.run_loss_buildings,
                activities.run_loss_population,
                activities.run_loss_casualties,
                activities.run_loss_economic,
                activities.run_loss_resources,
                activities.run_loss_validate,
                activities.mark_deadline_exceeded,
                activities.observe_task_deadlines,
                activities.finalize_assessment,
                activities.mark_artifact_production_launched,
                *_artifact_activities(session_factory),
            ],
        ):
            result = await environment.client.execute_workflow(
                AssessmentWorkflow.run,
                request,
                id=f"assessment-deadline:{request.event_id}",
                task_queue="assessment-deadline-test",
            )

    async with session_factory() as session:
        run = await session.get(AssessmentRun, result.run_id)

    assert run.status == "completed"
    assert run.completed_at is not None
    assert run.deadline_exceeded_at is not None


async def test_instrument_failure_still_completes_model_only(
    session_factory,
) -> None:
    request = await _create_workflow_input(session_factory)
    activities = AssessmentActivities(
        session_factory,
        intensity_service_factory=lambda: _DegradedInstrumentService(
            _test_intensity_service(session_factory)
        ),
        loss_service_factory=lambda: _test_loss_service(session_factory),
    )

    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=_TEST_SERVER,
    ) as environment:
        async with Worker(
            environment.client,
            task_queue="assessment-instrument-failure-test",
            workflows=[AssessmentWorkflow, ArtifactProductionWorkflow],
            activities=[
                activities.prepare_assessment,
                activities.run_intensity_model,
                activities.run_intensity_instrument,
                activities.run_intensity_fusion,
                activities.run_loss_buildings,
                activities.run_loss_population,
                activities.run_loss_casualties,
                activities.run_loss_economic,
                activities.run_loss_resources,
                activities.run_loss_validate,
                activities.mark_deadline_exceeded,
                activities.observe_task_deadlines,
                activities.finalize_assessment,
                activities.mark_artifact_production_launched,
                *_artifact_activities(session_factory),
            ],
        ):
            result = await environment.client.execute_workflow(
                AssessmentWorkflow.run,
                request,
                id=f"assessment-instrument-failure:{request.event_id}",
                task_queue="assessment-instrument-failure-test",
            )

    async with session_factory() as session:
        run = await session.get(AssessmentRun, result.run_id)
        tasks = (
            await session.scalars(
                select(AssessmentTask)
                .where(AssessmentTask.run_id == result.run_id)
                .order_by(AssessmentTask.sequence)
            )
        ).all()

    assert run.status == "completed"
    statuses = {task.task_key: task.status for task in tasks}
    assert statuses["intensity.model"] == "succeeded"
    assert statuses["intensity.instrument"] == "skipped"
    assert statuses["intensity.fusion"] == "succeeded"


async def test_building_failure_prevents_casualty_and_economic_activities(
    session_factory,
) -> None:
    calls = []

    class FailingLossService:
        async def run_buildings(self, run_id):
            calls.append("buildings")
            raise ValueError("vulnerability row unavailable")

        async def run_population(self, run_id):
            calls.append("population")
            return _loss_outcome(run_id, "loss.population")

    result = await _execute_loss_workflow(session_factory, FailingLossService())
    assert result.status == "failed"
    assert "casualties" not in calls
    assert "economic" not in calls


@pytest.mark.parametrize("status", ("failed", "skipped", "canceled"))
async def test_launch_marker_rejects_terminal_task(
    session_factory,
    status: str,
) -> None:
    request = await _create_workflow_input(session_factory)
    activities = AssessmentActivities(
        session_factory,
        intensity_service_factory=lambda: _test_intensity_service(session_factory),
        loss_service_factory=lambda: _test_loss_service(session_factory),
    )
    prepared = await activities.prepare_assessment(request)

    async with session_factory() as session:
        async with session.begin():
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == prepared.run_id,
                    AssessmentTask.task_key == "artifact.production",
                )
            )
            assert task is not None
            task.status = status

    with pytest.raises(ValueError, match="terminal"):
        await activities.mark_artifact_production_launched(
            AssessmentRunActivityInput(prepared.run_id)
        )
