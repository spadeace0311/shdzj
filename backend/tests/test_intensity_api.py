from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

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
from app.intensity.domain import GridDefinition, ProductStatus, ProductType
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.intensity.repository import IntensityProductWrite, IntensityRepository
from app.main import app
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_api_data(session_factory):
    await engine.dispose()
    await _delete(session_factory)
    yield
    await _delete(session_factory)
    await engine.dispose()


async def _delete(session_factory):
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(IntensityRaster))
            await session.execute(delete(IntensityFieldProduct))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))


async def _seed_product(session_factory) -> UUID:
    event = NormalizedEvent(
        EventKind.FORMAL,
        "cenc",
        "API-INTENSITY-1",
        datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        Decimal("121.5"),
        Decimal("31.2"),
        Decimal("10"),
        Decimal("5.2"),
        "test",
        datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "API-INTENSITY-1", "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            event.magnitude,
            event.depth_km,
            True,
            0,
            None,
            None,
        ),
        region_context=RegionContext(True, 0, "grid-test", received),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
            await IntensityRepository().save_product(
                session,
                IntensityProductWrite(
                    run_id=run.id,
                    task_id=task.id,
                    product_type=ProductType.MODEL,
                    status=ProductStatus.AVAILABLE,
                    algorithm_version="model-axis-ratio-v1",
                    parameter_version="shanghai-2019.1",
                    strategy_version=None,
                    grid_definition=GridDefinition(
                        "grid-test",
                        "EPSG:32651",
                        1000,
                        0,
                        1000,
                        1,
                        1,
                    ),
                    region_profile_version="shanghai-v1",
                    input_fingerprint="f" * 64,
                    input_checksum="i" * 64,
                    quality_grade=None,
                    coverage_ratio=0.0,
                    statistics={"minimum": 5.0, "maximum": 5.0, "mean": 5.0},
                    source_product_id=None,
                    observed_at=received,
                    bands=[("value", __import__("numpy").array([[5.0]]))],
                ),
            )
            return run.id


async def _seed_superseding_run(session_factory) -> UUID:
    event = NormalizedEvent(
        EventKind.CORRECTION,
        "cenc",
        "API-INTENSITY-1",
        datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        Decimal("121.5"),
        Decimal("31.2"),
        Decimal("10"),
        Decimal("5.3"),
        "test",
        datetime(2026, 9, 27, 1, 4, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 5, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "API-INTENSITY-1", "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            event.magnitude,
            event.depth_km,
            True,
            0,
            None,
            None,
        ),
        region_context=RegionContext(True, 0, "grid-test", received),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
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


async def test_get_intensity_result_summary(session_factory) -> None:
    run_id = await _seed_product(session_factory)
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
            response = await client.get(
                f"/api/v1/assessments/runs/{run_id}/intensity"
            )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(run_id)
    assert body["run_status"] in {"pending", "running", "completed", "failed"}
    assert body["effective_run_id"] is None
    assert body["products"][0]["product_type"] == "model"
    assert body["products"][0]["product_id"]
    assert body["products"][0]["algorithm_version"] == "model-axis-ratio-v1"


async def test_get_intensity_result_for_superseded_run(session_factory) -> None:
    first_run_id = await _seed_product(session_factory)
    repository = AssessmentRepository()

    async with session_factory() as session:
        async with session.begin():
            first = await session.get(AssessmentRun, first_run_id)
            event_id = first.event_id
            first_revision_id = first.revision_id
            await repository.start_run(session, first_run_id)
            await _complete_required_tasks(session, first_run_id)
            await repository.complete_run(session, first_run_id, "bundle-1")

    second_run_id = await _seed_superseding_run(session_factory)

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
            response = await client.get(
                f"/api/v1/assessments/runs/{first_run_id}/intensity"
            )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(first_run_id)
    assert body["event_id"] == str(event_id)
    assert body["revision_id"] == str(first_revision_id)
    assert body["run_status"] == "completed"
    assert body["superseded_by_run_id"] == str(second_run_id)
    assert body["effective_run_id"] == str(first_run_id)
    assert body["effective_revision_id"] == str(first_revision_id)
    assert body["is_latest_revision"] is False
    assert body["is_fallback"] is False
    assert body["products"][0]["product_type"] == "model"
