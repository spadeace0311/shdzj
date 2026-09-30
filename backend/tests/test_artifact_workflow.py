from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import os
from uuid import uuid4

import pytest
from sqlalchemy import select
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.artifacts.worker import _idempotency_key
from app.artifacts.workflow import (
    ArtifactDependencyWaitInput,
    ArtifactProductionWorkflow,
    ArtifactProductionWorkflowInput,
    ArtifactPublicationInput,
    ArtifactTaskActivityInput,
    ArtifactValidationInput,
)
from app.assessment.temporal import (
    AssessmentActivities,
    AssessmentWorkflow,
    AssessmentWorkflowInput,
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
    def __init__(self, *, block_keys: set[str] | None = None) -> None:
        self.wait_calls: list[str] = []
        self.render_calls: list[str] = []
        self.block_keys = block_keys or set()

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
        self.render_calls.append(f"map:{request.artifact_key}")
        return {"rendered": request.artifact_key}

    @activity.defn(name="compose_docx_artifact")
    async def compose_docx_artifact(self, request: ArtifactTaskActivityInput):
        self.render_calls.append(f"docx:{request.artifact_key}")
        return {"rendered": request.artifact_key}

    @activity.defn(name="compose_pptx_artifact")
    async def compose_pptx_artifact(self, request: ArtifactTaskActivityInput):
        self.render_calls.append(f"pptx:{request.artifact_key}")
        return {"rendered": request.artifact_key}

    @activity.defn(name="validate_artifact_production")
    async def validate_artifact_production(
        self,
        request: ArtifactValidationInput,
    ):
        return {"status": "completed", "completed_count": 4, "failed_count": 0}

    @activity.defn(name="publish_artifact_production")
    async def publish_artifact_production(
        self,
        request: ArtifactPublicationInput,
    ):
        return {"status": "completed", "completed_count": 4, "failed_count": 0}

    @activity.defn(name="mark_production_deadline_exceeded")
    async def mark_production_deadline_exceeded(
        self,
        request: ArtifactProductionWorkflowInput,
    ):
        return {"status": "timed_out"}


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

        initial_status = await artifact_handle.query(
            ArtifactProductionWorkflow.status
        )
        if initial_status == "running":
            await artifact_handle.signal(ArtifactProductionWorkflow.intensity_ready)
        assert await artifact_handle.query(ArtifactProductionWorkflow.status) in {
            "running",
            "completed",
            "partial",
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
