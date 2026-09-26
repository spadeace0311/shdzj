from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.temporal import (
    AssessmentActivities,
    AssessmentWorkflow,
    AssessmentWorkflowInput,
)
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.regions.domain import RegionContext


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


async def test_assessment_workflow_prepares_run_and_tasks(session_factory) -> None:
    request = await _create_workflow_input(session_factory)

    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with Worker(
            environment.client,
            task_queue="assessment-test",
            workflows=[AssessmentWorkflow],
            activities=[AssessmentActivities(session_factory).prepare_assessment],
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

    assert result.task_count == 9
    assert run is not None
    assert task_count == 9
