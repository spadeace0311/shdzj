from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.main import app
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_assessment_api_data(session_factory):
    await engine.dispose()
    await _delete_assessment_api_data(session_factory)
    yield
    await _delete_assessment_api_data(session_factory)
    await engine.dispose()


async def _delete_assessment_api_data(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))


async def _seed_assessment(session_factory) -> tuple[UUID, UUID]:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="CENC-ASSESSMENT-API-1",
        origin_time=datetime(2026, 9, 26, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="上海测试位置",
        report_time=datetime(2026, 9, 26, 1, 2, tzinfo=UTC),
    )
    received_at = datetime(2026, 9, 26, 1, 3, tzinfo=UTC)
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
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            assert outbox is not None
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            run_id = run.id
    return UUID(outcome.event_id), run_id


async def _seed_superseding_assessment(session_factory) -> UUID:
    event = NormalizedEvent(
        kind=EventKind.CORRECTION,
        source="cenc",
        source_event_id="CENC-ASSESSMENT-API-1",
        origin_time=datetime(2026, 9, 26, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.3"),
        place="上海测试位置",
        report_time=datetime(2026, 9, 26, 1, 4, tzinfo=UTC),
    )
    received_at = datetime(2026, 9, 26, 1, 5, tzinfo=UTC)
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
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            assert outbox is not None
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            return run.id


async def _complete_required_tasks(session, run_id) -> None:
    repository = AssessmentRepository()
    for task_key, algorithm_version, fingerprint in (
        ("intensity.model", "model-axis-ratio-v1", "a" * 64),
        ("intensity.fusion", "fusion-inverse-variance-v1", "b" * 64),
    ):
        task = await session.scalar(
            select(AssessmentTask).where(
                AssessmentTask.run_id == run_id,
                AssessmentTask.task_key == task_key,
            )
        )
        await repository.start_task(
            session,
            run_id,
            task_key,
            algorithm_version,
            fingerprint,
        )
        await repository.complete_task(
            session,
            task.id,
            fingerprint,
            {"product_id": task_key},
        )


async def _request_assessment(event_id: str):
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="operator",
        role="group_member",
        workgroup="震害评估组",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.get(f"/api/v1/assessments/events/{event_id}/current")
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


async def test_get_current_assessment_returns_run_and_task_progress(session_factory) -> None:
    event_id, run_id = await _seed_assessment(session_factory)

    response = await _request_assessment(str(event_id))

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(run_id)
    assert body["event_id"] == str(event_id)
    assert body["run_no"] == 1
    assert body["status"] == "pending"
    assert body["t1_at"] == "2026-09-26T01:03:00Z"
    assert body["deadline_at"] == "2026-09-26T01:08:00Z"
    assert body["completed_task_count"] == 0
    assert body["failed_task_count"] == 0
    assert body["total_task_count"] == 9
    assert [task["task_key"] for task in body["tasks"]] == [
        "intensity.model",
        "intensity.instrument",
        "intensity.fusion",
        "loss.population",
        "loss.casualties",
        "loss.buildings",
        "loss.economic",
        "report.rapid_assessment",
        "workgroup.response_tasks",
    ]
    assert body["intensity"]["run_id"] == str(run_id)
    assert body["intensity"]["event_id"] == str(event_id)
    assert body["intensity"]["run_status"] == "pending"
    assert body["intensity"]["effective_run_id"] is None
    assert body["intensity"]["products"] == []


async def test_get_current_assessment_returns_404_when_run_does_not_exist() -> None:
    response = await _request_assessment(str(uuid4()))

    assert response.status_code == 404
    assert response.json() == {"detail": "assessment_run_not_found"}


async def test_get_current_assessment_rejects_invalid_event_id() -> None:
    response = await _request_assessment("not-a-uuid")

    assert response.status_code == 422


async def test_get_current_assessment_uses_effective_run_for_fallback(
    session_factory,
) -> None:
    event_id, first_run_id = await _seed_assessment(session_factory)
    repository = AssessmentRepository()

    async with session_factory() as session:
        async with session.begin():
            await repository.start_run(session, first_run_id)
            await _complete_required_tasks(session, first_run_id)
            completed = await repository.complete_run(
                session,
                first_run_id,
                "bundle-1",
            )
            first_revision_id = completed.revision_id

    second_run_id = await _seed_superseding_assessment(session_factory)

    response = await _request_assessment(str(event_id))

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(second_run_id)
    assert body["intensity"]["run_id"] == str(first_run_id)
    assert body["intensity"]["event_id"] == str(event_id)
    assert body["intensity"]["run_status"] == "completed"
    assert body["intensity"]["effective_run_id"] == str(first_run_id)
    assert body["intensity"]["effective_revision_id"] == str(first_revision_id)
    assert body["intensity"]["is_latest_revision"] is False
    assert body["intensity"]["is_fallback"] is True
    assert body["intensity"]["products"] == []
