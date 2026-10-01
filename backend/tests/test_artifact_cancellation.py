from datetime import UTC, datetime, timedelta
from decimal import Decimal
import asyncio
import os

import pytest
from sqlalchemy import text
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.artifacts import worker as artifact_worker
from app.artifacts.dispatcher import (
    ArtifactProductionDispatcher,
    TemporalArtifactCancellationStarter,
)
from app.artifacts.models import ArtifactProductionCancelRequest
from app.artifacts.models import ProductionRun
from app.artifacts.repository import ArtifactProductionRepository
from app.artifacts.workflow import (
    ArtifactDependencyWaitInput,
    ArtifactProductionWorkflow,
    ArtifactProductionWorkflowInput,
    ArtifactTaskActivityInput,
    ArtifactTerminalizationInput,
)
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeRevision
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _set_current_order(
    session_factory,
    seeded_artifact_assessment,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            revision = await session.get(
                EarthquakeRevision,
                seeded_artifact_assessment.revision_id,
            )
            assert revision is not None
            revision.source_report_time = seeded_artifact_assessment.deadline_basis_at
            revision.source_report_number = 1


def _transition(
    seeded_artifact_assessment,
    *,
    kind: EventKind,
    report_number: int,
) -> dict[str, object]:
    received_at = seeded_artifact_assessment.deadline_basis_at + timedelta(
        minutes=report_number + 1
    )
    event = NormalizedEvent(
        kind=kind,
        source="artifact-fixture",
        source_event_id=f"formal-{seeded_artifact_assessment.event_id}",
        origin_time=seeded_artifact_assessment.deadline_basis_at,
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.3"),
        place="artifact fixture transition",
        report_time=received_at - timedelta(seconds=30),
        report_number=report_number,
    )
    return {
        "raw_payload": {
            "EventID": event.source_event_id,
            "type": "reviewed",
            "transition": report_number,
        },
        "event": event,
        "provider": "fan",
        "lane": "websocket",
        "received_at": received_at,
        "response_input": ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            deaths=None,
            max_intensity=Decimal("6"),
        ),
        "region_context": RegionContext(
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            boundary_version="artifact-cancellation-boundary",
            computed_at=received_at,
        ),
    }


async def _cancel_request_rows(session_factory, run_id):
    async with session_factory() as session:
        return (
            (
                await session.execute(
                    text(
                        """
                        SELECT workflow_id, reason, status
                        FROM artifact_production_cancel_requests
                        WHERE production_run_id = :production_run_id
                        """
                    ),
                    {"production_run_id": run_id},
                )
            )
            .mappings()
            .all()
        )


async def test_correction_service_enqueues_and_cancels_old_production(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    repository = ArtifactProductionRepository()
    old = await seeded_artifact_assessment.create_full_run()
    await _set_current_order(session_factory, seeded_artifact_assessment)
    transition = _transition(
        seeded_artifact_assessment,
        kind=EventKind.CORRECTION,
        report_number=2,
    )

    await EventService(session_factory).ingest_collected(**transition)

    async with session_factory() as session:
        stored = await session.get(ProductionRun, old.id)
        tasks = await repository.list_tasks(session, old.id)

    assert stored is not None
    assert stored.status == "canceled"
    assert stored.cancel_reason == "revision_superseded"
    assert stored.is_current is False
    assert tasks
    assert all(task.status == "canceled" for task in tasks)
    rows = await _cancel_request_rows(session_factory, old.id)
    assert len(rows) == 1
    assert rows[0]["reason"] == "revision_superseded"
    assert rows[0]["status"] == "pending"


async def test_real_event_service_cancels_non_live_production(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    repository = ArtifactProductionRepository()
    manual = await seeded_artifact_assessment.create_full_run(
        production_mode="manual"
    )
    await _set_current_order(session_factory, seeded_artifact_assessment)
    transition = _transition(
        seeded_artifact_assessment,
        kind=EventKind.MANUAL,
        report_number=2,
    )

    await EventService(session_factory).ingest_collected(**transition)

    async with session_factory() as session:
        stored = await session.get(ProductionRun, manual.id)
        tasks = await repository.list_tasks(session, manual.id)

    assert stored is not None
    assert stored.status == "canceled"
    assert stored.cancel_reason == "real_event_priority"
    assert all(task.status == "canceled" for task in tasks)
    rows = await _cancel_request_rows(session_factory, manual.id)
    assert len(rows) == 1
    assert rows[0]["reason"] == "real_event_priority"
    assert rows[0]["status"] == "pending"


class _RecordingTemporalClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_workflow_handle(self, workflow_id: str):
        return _RecordingWorkflowHandle(self, workflow_id)


class _RecordingWorkflowHandle:
    def __init__(self, client: _RecordingTemporalClient, workflow_id: str) -> None:
        self._client = client
        self.workflow_id = workflow_id

    async def signal(self, method: object, reason: str) -> None:
        del method
        self._client.calls.append((self.workflow_id, reason))


async def test_artifact_dispatcher_signals_durable_cancellation(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    async with session_factory() as session:
        async with session.begin():
            session.add(
                ArtifactProductionCancelRequest(
                    production_run_id=run.id,
                    workflow_id=f"artifact-production:{run.id}",
                    reason="revision_superseded",
                    status="pending",
                    available_at=datetime.now(UTC),
                )
            )
            await session.flush()

    client = _RecordingTemporalClient()
    stop_event = asyncio.Event()

    async def stop_after_dispatch(_seconds: float) -> None:
        stop_event.set()

    configured = type(
        "Configured",
        (),
        {
            "assessment_outbox_poll_seconds": 0.01,
            "assessment_outbox_batch_size": 10,
            "assessment_outbox_max_attempts": 3,
            "assessment_outbox_lease_seconds": 60,
        },
    )()
    await asyncio.wait_for(
        artifact_worker.run_artifact_dispatcher(
            stop_event,
            client=client,
            session_factory=session_factory,
            configured=configured,
            sleep=stop_after_dispatch,
        ),
        timeout=2,
    )

    assert client.calls == [
        (f"artifact-production:{run.id}", "revision_superseded")
    ]


class _BlockingArtifactActivities:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.render_calls: list[str] = []

    @activity.defn(name="prepare_artifact_production")
    async def prepare_artifact_production(
        self,
        request: ArtifactProductionWorkflowInput,
    ) -> dict[str, object]:
        return {
            "production_run_id": request.production_run_id,
            "deadline_at": request.deadline_at,
            "context_fingerprint": request.context_fingerprint,
            "launch_mode": "standalone",
            "background_outputs": [
                list(item) for item in request.required_outputs
            ],
            "intensity_outputs": [],
            "loss_core_outputs": [],
            "loss_final_outputs": [],
        }

    @activity.defn(name="wait_for_artifact_dependencies")
    async def wait_for_artifact_dependencies(
        self,
        request: ArtifactDependencyWaitInput,
    ) -> None:
        del request
        self.entered.set()
        await asyncio.sleep(3600)

    @activity.defn(name="render_map_artifact")
    async def render_map_artifact(
        self,
        request: ArtifactTaskActivityInput,
    ) -> None:
        del request
        self.render_calls.append("rendered")

    @activity.defn(name="compose_docx_artifact")
    async def compose_docx_artifact(
        self,
        request: ArtifactTaskActivityInput,
    ) -> None:
        del request
        self.render_calls.append("rendered")

    @activity.defn(name="compose_pptx_artifact")
    async def compose_pptx_artifact(
        self,
        request: ArtifactTaskActivityInput,
    ) -> None:
        del request
        self.render_calls.append("rendered")

    @activity.defn(name="cancel_artifact_production")
    async def cancel_artifact_production(
        self,
        request: ArtifactTerminalizationInput,
    ) -> dict[str, object]:
        del request
        return {
            "status": "canceled",
            "completed_count": 0,
            "failed_count": 1,
        }


@pytest.fixture
async def temporal_env():
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ.get(
            "TEMPORAL_TEST_SERVER_EXECUTABLE"
        )
    ) as environment:
        yield environment


async def test_temporal_dispatcher_cancels_live_old_production_chain(
    temporal_env,
    seeded_artifact_assessment,
    session_factory,
) -> None:
    old = await seeded_artifact_assessment.create_full_run()
    async with session_factory() as session:
        stored = await session.get(ProductionRun, old.id)
        assert stored is not None
        request = ArtifactProductionWorkflowInput(
            production_run_id=str(stored.id),
            assessment_run_id=str(
                stored.assessment_run_id
                if stored.assessment_run_id is not None
                else ""
            ),
            event_id=str(stored.event_id),
            revision_id=str(stored.revision_id),
            deadline_at=stored.deadline_at.isoformat(),
            catalog_version=stored.catalog_version,
            context_fingerprint="",
            launch_mode="standalone",
            generation_seq=stored.generation_seq,
            generation_scope=stored.generation_scope,
            required_outputs=tuple(
                (item["artifact_key"], item["output_profile"])
                for item in stored.required_outputs
            ),
            render_concurrency=1,
            deadline_basis_at=stored.deadline_basis_at.isoformat(),
        )

    probe = _BlockingArtifactActivities()
    queue = f"cancellation-{seeded_artifact_assessment.event_id}"
    handle = await temporal_env.client.start_workflow(
        ArtifactProductionWorkflow.run,
        request,
        id=f"artifact-production:{old.id}",
        task_queue=queue,
    )
    async with Worker(
        temporal_env.client,
        task_queue=queue,
        workflows=[ArtifactProductionWorkflow],
        activities=[
            probe.prepare_artifact_production,
            probe.wait_for_artifact_dependencies,
            probe.render_map_artifact,
            probe.compose_docx_artifact,
            probe.compose_pptx_artifact,
            probe.cancel_artifact_production,
        ],
        disable_eager_activity_execution=True,
    ):
        await asyncio.wait_for(probe.entered.wait(), timeout=5)
        repository = ArtifactProductionRepository()
        async with session_factory() as session:
            async with session.begin():
                await repository.create_run(
                    session,
                    seeded_artifact_assessment.full_run_command(
                        generation_seq=2,
                        snapshot={"fixture": "temporal-replacement"},
                        reuse_existing=False,
                    ),
                )
        dispatcher = ArtifactProductionDispatcher(
            session_factory=session_factory,
            starter=TemporalArtifactCancellationStarter(
                temporal_env.client
            ),
            batch_size=10,
            max_attempts=3,
            lease_seconds=60,
        )
        published = await dispatcher.dispatch_once()
        result = await asyncio.wait_for(handle.result(), timeout=10)

    assert published == 1
    assert result.status == "canceled"
    assert probe.render_calls == []
    async with session_factory() as session:
        stored = await session.get(ProductionRun, old.id)
        cancel = await session.scalar(
            text(
                """
                SELECT status
                FROM artifact_production_cancel_requests
                WHERE production_run_id = :production_run_id
                """
            ),
            {"production_run_id": old.id},
        )
    assert stored is not None
    assert stored.status == "canceled"
    assert stored.cancel_reason == "same_scope_replacement"
    assert cancel == "published"
