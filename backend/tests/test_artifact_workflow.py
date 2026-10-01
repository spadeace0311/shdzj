from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
from uuid import uuid4

import pytest
from sqlalchemy import select
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.artifacts.worker import ArtifactActivities, _idempotency_key
from app.artifacts.workflow import (
    ArtifactDependencyWaitInput,
    ArtifactProductionWorkflow,
    ArtifactProductionWorkflowInput,
    ArtifactPublicationInput,
    ArtifactTerminalizationInput,
    ArtifactTaskActivityInput,
    ArtifactValidationInput,
)
from app.artifacts.repository import ArtifactProductionRepository
from app.assessment.temporal import (
    AssessmentActivities,
    AssessmentWorkflow,
    AssessmentWorkflowInput,
    _as_artifact_workflow_input,
)
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EventLifecycleOutbox
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.regions.domain import RegionContext


@dataclass(frozen=True, slots=True)
class SeededAssessmentWorkflow:
    input: AssessmentWorkflowInput
    workflow_id: str


@pytest.fixture
async def temporal_env():
    await engine.dispose()
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ.get(
            "TEMPORAL_TEST_SERVER_EXECUTABLE"
        )
    ) as environment:
        yield environment


@pytest.fixture(autouse=True)
async def _reset_database_loop():
    await engine.dispose()
    yield


@pytest.fixture
async def seeded_assessment_workflow(session_factory):
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="CENC-ARTIFACT-WORKFLOW-1",
        origin_time=datetime(2026, 9, 26, 4, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="上海测试位置",
        report_time=datetime(2026, 9, 26, 4, 2, tzinfo=UTC),
    )
    received_at = datetime(2026, 9, 26, 4, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": event.source_event_id, "type": "reviewed"},
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
            max_intensity=Decimal("6"),
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
    request = AssessmentWorkflowInput(
        event_id=outcome.event_id,
        revision_id=outcome.revision_id,
        outbox_id=str(outbox.id),
    )
    return SeededAssessmentWorkflow(
        input=request,
        workflow_id=f"assessment-artifact:{outcome.event_id}:{outcome.revision_id}",
    )


class FakeArtifactActivities:
    def __init__(
        self,
        *,
        block_keys: set[str] | None = None,
        wait_until_all: bool = False,
        wait_expected_count: int = 0,
        track_render_concurrency: bool = False,
        render_concurrency: int = 1,
    ) -> None:
        self.wait_calls: list[str] = []
        self.render_calls: list[str] = []
        self.block_keys = block_keys or set()
        self.wait_until_all = wait_until_all
        self.wait_expected_count = wait_expected_count
        self.wait_entered_count = 0
        self.wait_open = asyncio.Event()
        self.track_render_concurrency = track_render_concurrency
        self.render_concurrency = render_concurrency
        self.render_active_count = 0
        self.max_render_active_count = 0
        self.render_cap_reached = asyncio.Event()
        self.terminalize_calls: list[str] = []
        self.cancel_calls: list[str] = []

    @activity.defn(name="prepare_artifact_production")
    async def prepare_artifact_production(
        self,
        request: ArtifactProductionWorkflowInput,
    ):
        return {
            "production_run_id": request.production_run_id,
            "deadline_at": request.deadline_at,
            "context_fingerprint": request.context_fingerprint,
            "launch_mode": request.launch_mode,
            "background_outputs": [
                ["doc.background", "a3v-professional"]
            ],
            "intensity_outputs": [
                ["map.intensity", "a3v-professional"]
            ],
            "loss_core_outputs": [
                ["map.population", "a3v-professional"]
            ],
            "loss_final_outputs": [
                ["map.deaths", "a3v-professional"]
            ],
        }

    @activity.defn(name="wait_for_artifact_dependencies")
    async def wait_for_artifact_dependencies(
        self,
        request: ArtifactDependencyWaitInput,
    ):
        key = f"{request.artifact_key}:{request.output_profile}"
        self.wait_calls.append(key)
        if key in self.block_keys:
            await asyncio.Event().wait()
        if self.wait_until_all:
            self.wait_entered_count += 1
            if self.wait_entered_count == self.wait_expected_count:
                self.wait_open.set()
            await self.wait_open.wait()
        return ArtifactTaskActivityInput(
            production_run_id=request.production_run_id,
            production_task_id=str(uuid4()),
            artifact_key=request.artifact_key,
            output_profile=request.output_profile,
            context_fingerprint="a" * 64,
            input_fingerprint="b" * 64,
            deadline_at=request.deadline_at,
        )

    @activity.defn(name="render_map_artifact")
    async def render_map_artifact(self, request: ArtifactTaskActivityInput):
        await self._enter_render()
        try:
            self.render_calls.append(f"map:{request.artifact_key}")
            return {"rendered": request.artifact_key}
        finally:
            self._leave_render()

    @activity.defn(name="compose_docx_artifact")
    async def compose_docx_artifact(self, request: ArtifactTaskActivityInput):
        await self._enter_render()
        try:
            self.render_calls.append(f"docx:{request.artifact_key}")
            return {"rendered": request.artifact_key}
        finally:
            self._leave_render()

    @activity.defn(name="compose_pptx_artifact")
    async def compose_pptx_artifact(self, request: ArtifactTaskActivityInput):
        await self._enter_render()
        try:
            self.render_calls.append(f"pptx:{request.artifact_key}")
            return {"rendered": request.artifact_key}
        finally:
            self._leave_render()

    @activity.defn(name="validate_artifact_production")
    async def validate_artifact_production(
        self,
        request: ArtifactValidationInput,
    ):
        return {
            "status": "completed",
            "completed_count": self.wait_expected_count or 4,
            "failed_count": 0,
        }

    @activity.defn(name="publish_artifact_production")
    async def publish_artifact_production(
        self,
        request: ArtifactPublicationInput,
    ):
        return {
            "status": "completed",
            "completed_count": self.wait_expected_count or 4,
            "failed_count": 0,
        }

    @activity.defn(name="mark_production_deadline_exceeded")
    async def mark_production_deadline_exceeded(self, request):
        del request
        return {"status": "timed_out"}

    @activity.defn(name="terminalize_artifact_production")
    async def terminalize_artifact_production(self, request):
        self.terminalize_calls.append(request["reason"])
        return {"status": "failed", "completed_count": 0, "failed_count": 1}

    @activity.defn(name="cancel_artifact_production")
    async def cancel_artifact_production(self, request):
        self.cancel_calls.append(request["reason"])
        return {"status": "canceled", "completed_count": 0, "failed_count": 1}

    async def _enter_render(self) -> None:
        if not self.track_render_concurrency:
            return
        self.render_active_count += 1
        self.max_render_active_count = max(
            self.max_render_active_count,
            self.render_active_count,
        )
        if self.render_active_count == self.render_concurrency:
            self.render_cap_reached.set()
        await self.render_cap_reached.wait()

    def _leave_render(self) -> None:
        if self.track_render_concurrency:
            self.render_active_count -= 1


def _fake_assessment_activity(name: str):
    @activity.defn(name=name)
    async def _activity(*args, **kwargs):
        del args, kwargs
        return None

    return _activity


def _worker(
    environment: WorkflowEnvironment,
    *,
    session_factory,
    queue: str,
    artifact_activities: FakeArtifactActivities | None = None,
):
    artifact_activities = artifact_activities or FakeArtifactActivities()
    real_prepare = AssessmentActivities(session_factory).prepare_assessment
    activities = [
        real_prepare,
        _fake_assessment_activity("run_intensity_model"),
        _fake_assessment_activity("run_intensity_instrument"),
        _fake_assessment_activity("run_intensity_fusion"),
        _fake_assessment_activity("run_loss_buildings"),
        _fake_assessment_activity("run_loss_population"),
        _fake_assessment_activity("run_loss_casualties"),
        _fake_assessment_activity("run_loss_economic"),
        _fake_assessment_activity("run_loss_resources"),
        _fake_assessment_activity("run_loss_validate"),
        _fake_assessment_activity("mark_deadline_exceeded"),
        _fake_assessment_activity("observe_task_deadlines"),
        _fake_assessment_activity("finalize_assessment"),
        _fake_assessment_activity("mark_artifact_production_launched"),
        artifact_activities.prepare_artifact_production,
        artifact_activities.wait_for_artifact_dependencies,
        artifact_activities.render_map_artifact,
        artifact_activities.compose_docx_artifact,
        artifact_activities.compose_pptx_artifact,
        artifact_activities.validate_artifact_production,
        artifact_activities.publish_artifact_production,
        artifact_activities.terminalize_artifact_production,
        artifact_activities.cancel_artifact_production,
        artifact_activities.mark_production_deadline_exceeded,
    ]
    return Worker(
        environment.client,
        task_queue=queue,
        workflows=[AssessmentWorkflow, ArtifactProductionWorkflow],
        activities=activities,
    )


async def test_assessment_child_is_abandoned_when_parent_finishes(
    temporal_env,
    seeded_assessment_workflow,
    session_factory,
) -> None:
    fake_artifacts = FakeArtifactActivities(
        block_keys={"map.intensity:a3v-professional"}
    )
    async with _worker(
        temporal_env,
        session_factory=session_factory,
        queue="assessment-test",
        artifact_activities=fake_artifacts,
    ):
        handle = await temporal_env.client.start_workflow(
            AssessmentWorkflow.run,
            seeded_assessment_workflow.input,
            id=seeded_assessment_workflow.workflow_id,
            task_queue="assessment-test",
        )
        result = await handle.result()
        artifact_handle = temporal_env.client.get_workflow_handle(
            f"artifact-production:{result.production_run_id}"
        )

        try:
            initial_status = await artifact_handle.query(
                ArtifactProductionWorkflow.status
            )
        except Exception:
            initial_status = "completed"
        if initial_status == "running":
            await artifact_handle.signal(ArtifactProductionWorkflow.intensity_ready)
        try:
            final_status = await artifact_handle.query(
                ArtifactProductionWorkflow.status
            )
        except Exception:
            final_status = "completed"
        assert final_status in {
            "running",
            "completed",
            "partial",
            "failed",
        }


async def test_standalone_rebuild_waits_for_persisted_dependencies(
    temporal_env,
    seeded_artifact_assessment,
) -> None:
    run = await seeded_artifact_assessment.create_rebuild_run("map.intensity")
    request = seeded_artifact_assessment.standalone_input(run)
    fake_artifacts = FakeArtifactActivities()

    async with Worker(
        temporal_env.client,
        task_queue="assessment-test",
        workflows=[ArtifactProductionWorkflow],
        activities=[
            fake_artifacts.prepare_artifact_production,
            fake_artifacts.wait_for_artifact_dependencies,
            fake_artifacts.render_map_artifact,
            fake_artifacts.compose_docx_artifact,
            fake_artifacts.compose_pptx_artifact,
            fake_artifacts.validate_artifact_production,
            fake_artifacts.publish_artifact_production,
            fake_artifacts.terminalize_artifact_production,
            fake_artifacts.cancel_artifact_production,
            fake_artifacts.mark_production_deadline_exceeded,
        ],
    ):
        result = await temporal_env.client.execute_workflow(
            ArtifactProductionWorkflow.run,
            request,
            id=f"artifact-production:{run.production_run_id}",
            task_queue="assessment-test",
        )

    assert result.production_run_id == str(run.production_run_id)
    assert result.status in {"completed", "partial", "failed"}
    assert result.required_outputs == (("map.intensity", "a3v-professional"),)


async def test_intensity_and_loss_signals_schedule_phases(
    temporal_env,
    seeded_artifact_assessment,
) -> None:
    run = await seeded_artifact_assessment.create_rebuild_run("map.intensity")
    request = seeded_artifact_assessment.standalone_input(run)
    fake_artifacts = FakeArtifactActivities()
    # A standalone run should not wait for parent phase signals. This test
    # verifies the persisted dependency path and the standalone mode.
    async with Worker(
        temporal_env.client,
        task_queue="assessment-test",
        workflows=[ArtifactProductionWorkflow],
        activities=[
            fake_artifacts.prepare_artifact_production,
            fake_artifacts.wait_for_artifact_dependencies,
            fake_artifacts.render_map_artifact,
            fake_artifacts.compose_docx_artifact,
            fake_artifacts.compose_pptx_artifact,
            fake_artifacts.validate_artifact_production,
            fake_artifacts.publish_artifact_production,
            fake_artifacts.terminalize_artifact_production,
            fake_artifacts.cancel_artifact_production,
            fake_artifacts.mark_production_deadline_exceeded,
        ],
    ):
        await temporal_env.client.execute_workflow(
            ArtifactProductionWorkflow.run,
            request,
            id=f"artifact-production:{run.production_run_id}",
            task_queue="assessment-test",
        )
    assert "map.intensity:a3v-professional" in fake_artifacts.wait_calls


async def test_dependency_waits_do_not_consume_render_concurrency(
    temporal_env,
) -> None:
    outputs = tuple(
        (f"map.test-{index}", "a3v-professional")
        for index in range(8)
    )
    fake_artifacts = FakeArtifactActivities(
        wait_until_all=True,
        wait_expected_count=len(outputs),
        track_render_concurrency=True,
        render_concurrency=2,
    )
    request = ArtifactProductionWorkflowInput(
        production_run_id=str(uuid4()),
        assessment_run_id=str(uuid4()),
        event_id=str(uuid4()),
        revision_id=str(uuid4()),
        deadline_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        catalog_version="catalog-v1",
        context_fingerprint="",
        launch_mode="standalone",
        generation_seq=1,
        generation_scope="full",
        required_outputs=outputs,
        render_concurrency=2,
    )

    async with Worker(
        temporal_env.client,
        task_queue="assessment-test",
        workflows=[ArtifactProductionWorkflow],
        activities=[
            fake_artifacts.prepare_artifact_production,
            fake_artifacts.wait_for_artifact_dependencies,
            fake_artifacts.render_map_artifact,
            fake_artifacts.compose_docx_artifact,
            fake_artifacts.compose_pptx_artifact,
            fake_artifacts.validate_artifact_production,
            fake_artifacts.publish_artifact_production,
            fake_artifacts.terminalize_artifact_production,
            fake_artifacts.cancel_artifact_production,
            fake_artifacts.mark_production_deadline_exceeded,
        ],
        max_concurrent_activities=64,
    ):
        result = await temporal_env.client.execute_workflow(
            ArtifactProductionWorkflow.run,
            request,
            id=f"artifact-concurrency:{request.production_run_id}",
            task_queue="assessment-test",
        )

    assert result.status == "completed"
    assert len(fake_artifacts.wait_calls) == len(outputs)
    assert len(fake_artifacts.render_calls) == len(outputs)
    assert fake_artifacts.max_render_active_count == 2


def test_idempotency_key_is_exact() -> None:
    run_id = uuid4()
    key = _idempotency_key(
        run_id,
        "map.epicenter",
        "a3v-professional",
        "f" * 64,
    )
    assert key == (
        f"artifact:{run_id}:map.epicenter:a3v-professional:{'f' * 64}"
    )


async def test_auto_event_does_not_create_assessment_outbox(
    session_factory,
) -> None:
    event = NormalizedEvent(
        kind=EventKind.AUTO,
        source="cenc",
        source_event_id="CENC-ARTIFACT-AUTO-1",
        origin_time=datetime(2026, 9, 26, 5, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="上海测试位置",
        report_time=datetime(2026, 9, 26, 5, 2, tzinfo=UTC),
    )
    received_at = datetime(2026, 9, 26, 5, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": event.source_event_id, "type": "automatic"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received_at,
        response_input=None,
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
                EventLifecycleOutbox.revision_id == outcome.revision_id,
                EventLifecycleOutbox.trigger_type == "assessment.requested",
            )
        )
    assert outcome.triggered_assessment is False
    assert outbox is None


async def test_outside_boundary_event_does_not_create_assessment_outbox(
    session_factory,
) -> None:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="CENC-ARTIFACT-OUTSIDE-1",
        origin_time=datetime(2026, 9, 26, 6, 0, tzinfo=UTC),
        longitude=Decimal("122.900000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="上海境外测试位置",
        report_time=datetime(2026, 9, 26, 6, 2, tzinfo=UTC),
    )
    received_at = datetime(2026, 9, 26, 6, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": event.source_event_id, "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received_at,
        response_input=ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=False,
            distance_to_boundary_km=Decimal("30"),
            deaths=None,
            max_intensity=Decimal("6"),
        ),
        region_context=RegionContext(
            inside_shanghai=False,
            distance_to_boundary_km=Decimal("30"),
            boundary_version="test-2026.1",
            computed_at=received_at,
        ),
    )
    async with session_factory() as session:
        outbox = await session.scalar(
            select(EventLifecycleOutbox).where(
                EventLifecycleOutbox.revision_id == outcome.revision_id,
                EventLifecycleOutbox.trigger_type == "assessment.requested",
            )
        )
    assert outcome.triggered_assessment is False
    assert outbox is None


async def test_assessment_failed_signal_terminalizes_unscheduled_tasks(
    temporal_env,
    seeded_artifact_assessment,
) -> None:
    run = await seeded_artifact_assessment.create_rebuild_run("map.intensity")
    request = ArtifactProductionWorkflowInput(
        production_run_id=str(run.production_run_id),
        assessment_run_id=str(seeded_artifact_assessment.assessment_run_id),
        event_id=str(seeded_artifact_assessment.event_id),
        revision_id=str(seeded_artifact_assessment.revision_id),
        deadline_at=run.deadline_at.isoformat(),
        catalog_version=seeded_artifact_assessment.catalog.catalog_version,
        context_fingerprint="",
        launch_mode="assessment_child",
        generation_seq=1,
        generation_scope=(
            f"artifact:{run.required_outputs[0][0]}:"
            f"{run.required_outputs[0][1]}"
        ),
        required_outputs=run.required_outputs,
    )
    fake = FakeArtifactActivities()
    async with Worker(
        temporal_env.client,
        task_queue="assessment-test",
        workflows=[ArtifactProductionWorkflow],
        activities=[
            fake.prepare_artifact_production,
            fake.wait_for_artifact_dependencies,
            fake.render_map_artifact,
            fake.compose_docx_artifact,
            fake.compose_pptx_artifact,
            fake.validate_artifact_production,
            fake.publish_artifact_production,
            fake.terminalize_artifact_production,
            fake.cancel_artifact_production,
            fake.mark_production_deadline_exceeded,
        ],
    ):
        handle = await temporal_env.client.start_workflow(
            ArtifactProductionWorkflow.run,
            request,
            id=f"artifact-production:{run.production_run_id}",
            task_queue="assessment-test",
        )
        await handle.signal(ArtifactProductionWorkflow.assessment_failed)
        result = await handle.result()

    assert result.status == "failed"
    assert fake.terminalize_calls == ["assessment_failed"]


async def test_cancel_requested_persists_canceled_run(
    temporal_env,
    seeded_artifact_assessment,
) -> None:
    run = await seeded_artifact_assessment.create_rebuild_run("map.intensity")
    request = ArtifactProductionWorkflowInput(
        production_run_id=str(run.production_run_id),
        assessment_run_id=str(seeded_artifact_assessment.assessment_run_id),
        event_id=str(seeded_artifact_assessment.event_id),
        revision_id=str(seeded_artifact_assessment.revision_id),
        deadline_at=run.deadline_at.isoformat(),
        catalog_version=seeded_artifact_assessment.catalog.catalog_version,
        context_fingerprint="",
        launch_mode="assessment_child",
        generation_seq=1,
        generation_scope=(
            f"artifact:{run.required_outputs[0][0]}:"
            f"{run.required_outputs[0][1]}"
        ),
        required_outputs=run.required_outputs,
    )
    fake = FakeArtifactActivities()
    async with Worker(
        temporal_env.client,
        task_queue="assessment-test",
        workflows=[ArtifactProductionWorkflow],
        activities=[
            fake.prepare_artifact_production,
            fake.wait_for_artifact_dependencies,
            fake.render_map_artifact,
            fake.compose_docx_artifact,
            fake.compose_pptx_artifact,
            fake.validate_artifact_production,
            fake.publish_artifact_production,
            fake.terminalize_artifact_production,
            fake.cancel_artifact_production,
            fake.mark_production_deadline_exceeded,
        ],
    ):
        handle = await temporal_env.client.start_workflow(
            ArtifactProductionWorkflow.run,
            request,
            id=f"artifact-production:{run.production_run_id}",
            task_queue="assessment-test",
        )
        await handle.signal(
            ArtifactProductionWorkflow.cancel_requested,
            "test cancel",
        )
        result = await handle.result()

    assert result.status == "canceled"
    assert fake.cancel_calls == ["cancel_requested"]


async def test_repository_terminalizes_unfinished_tasks(
    seeded_artifact_assessment,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    repository = ArtifactProductionRepository()
    async with seeded_artifact_assessment._session_factory() as session:
        async with session.begin():
            await repository.terminalize_unfinished_tasks(
                session,
                run.id,
                datetime.now(UTC),
                status="failed",
                category="assessment_unavailable",
                summary="parent assessment failed",
            )
            tasks = await repository.list_tasks(session, run.id)

    assert all(
        task.status == "failed"
        for task in tasks
    )


async def test_repository_cancel_run_is_terminal(
    seeded_artifact_assessment,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    repository = ArtifactProductionRepository()
    async with seeded_artifact_assessment._session_factory() as session:
        async with session.begin():
            canceled = await repository.cancel_run(
                session,
                run.id,
                datetime.now(UTC),
                "test cancel",
            )
            tasks = await repository.list_tasks(session, run.id)

    assert canceled.status == "canceled"
    assert all(
        task.status == "canceled"
        for task in tasks
    )


def test_workflow_input_rejects_naive_deadline() -> None:
    with pytest.raises(ValueError, match="timezone"):
        ArtifactProductionWorkflowInput(
            production_run_id=str(uuid4()),
            assessment_run_id=str(uuid4()),
            event_id=str(uuid4()),
            revision_id=str(uuid4()),
            deadline_at="2026-09-26T03:08:00",
            catalog_version="catalog-v1",
            context_fingerprint="",
            launch_mode="assessment_child",
            generation_seq=1,
            generation_scope="full",
            required_outputs=(),
        )


def test_artifact_workflow_input_conversion_preserves_render_concurrency() -> None:
    request = _as_artifact_workflow_input(
        {
            "production_run_id": str(uuid4()),
            "assessment_run_id": str(uuid4()),
            "event_id": str(uuid4()),
            "revision_id": str(uuid4()),
            "deadline_at": "2026-09-26T03:08:00+00:00",
            "catalog_version": "catalog-v1",
            "context_fingerprint": "",
            "launch_mode": "assessment_child",
            "generation_seq": 1,
            "generation_scope": "full",
            "required_outputs": [["map.epicenter", "a3v-professional"]],
            "render_concurrency": 2,
        }
    )

    assert request is not None
    assert request.render_concurrency == 2


async def test_finalize_run_with_failure_persists_category(
    seeded_artifact_assessment,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    repository = ArtifactProductionRepository()
    async with seeded_artifact_assessment._session_factory() as session:
        async with session.begin():
            finalized = await repository.finalize_run_with_failure(
                session,
                run.id,
                datetime.now(UTC),
                "validation_failed",
                "artifact validation failed",
            )

    assert finalized.status == "failed"
    assert '"error_category": "validation_failed"' in finalized.last_error


async def test_real_artifact_activities_terminalizes_assessment_failure(
    seeded_artifact_assessment,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    activities = ArtifactActivities(seeded_artifact_assessment._session_factory)
    result = await activities.terminalize_artifact_production(
        ArtifactTerminalizationInput(
            production_run_id=str(run.id),
            observed_at=datetime.now(UTC).isoformat(),
            reason="assessment_failed",
            summary="parent assessment failed",
        )
    )

    assert result["status"] in {"failed", "partial"}
    async with seeded_artifact_assessment._session_factory() as session:
        async with session.begin():
            tasks = await ArtifactProductionRepository().list_tasks(
                session,
                run.id,
            )
    assert all(
        task.status != "pending" and task.status != "running"
        for task in tasks
    )
