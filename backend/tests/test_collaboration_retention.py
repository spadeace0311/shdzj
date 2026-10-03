from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.collaboration.domain import WorkgroupCode
from app.collaboration.generation import CollaborationTaskGenerator
from app.collaboration.models import WorkgroupTask
from app.collaboration.retention import CollaborationRetentionService
from app.collaboration.templates import TaskTemplateCatalog, TaskTemplateDefinition
from app.config import settings
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)


@pytest.fixture
async def session():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            transaction = await session.begin()
            try:
                yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def isolated_session_factory():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest.fixture
def event_factory(session):
    async def factory(
        *,
        event_type: str,
        age_days: int = 0,
        lifecycle_state: str = "not_applicable",
        origin_time: datetime | None = None,
    ) -> EarthquakeEvent:
        origin_time = origin_time or (
            datetime(2026, 10, 3, tzinfo=UTC) - timedelta(days=age_days)
        )
        event = EarthquakeEvent(
            id=uuid.uuid4(),
            source="retention-test",
            canonical_source_id=f"retention-{uuid.uuid4().hex}",
            event_type=event_type,
            origin_time=origin_time,
            longitude=Decimal("121.500000"),
            latitude=Decimal("31.200000"),
            depth_km=Decimal("10.00"),
            magnitude=Decimal("5.2"),
            place="retention fixture",
            geom=WKTElement("POINT(121.5 31.2)", srid=4326),
            lifecycle_state=lifecycle_state,
        )
        session.add(event)
        await session.flush()
        return event

    return factory


@pytest.fixture
def revision_factory(session):
    async def factory(event: EarthquakeEvent) -> EarthquakeRevision:
        raw = RawMessage(
            id=uuid.uuid4(),
            source="retention-test",
            source_message_id=uuid.uuid4().hex,
            message_kind=event.event_type,
            checksum=uuid.uuid4().hex,
            payload={"event_id": str(event.id)},
        )
        session.add(raw)
        await session.flush()
        revision = EarthquakeRevision(
            id=uuid.uuid4(),
            event_id=event.id,
            raw_message_id=raw.id,
            revision_no=1,
            revision_kind=event.event_type,
            origin_time=event.origin_time,
            longitude=event.longitude,
            latitude=event.latitude,
            depth_km=event.depth_km,
            magnitude=event.magnitude,
            place=event.place,
            is_current=True,
            ingested_at=event.origin_time,
        )
        session.add(revision)
        await session.flush()
        return revision

    return factory


@pytest.fixture
def task_factory(session, revision_factory):
    async def factory(
        event: EarthquakeEvent,
        *,
        status: str = "completed",
    ) -> WorkgroupTask:
        revision = await revision_factory(event)
        task = WorkgroupTask(
            event_id=event.id,
            trigger_revision_id=revision.id,
            task_code=f"retention-{uuid.uuid4().hex}",
            source_type="ad_hoc",
            workgroup_code="monitoring_forecast",
            title="Retention fixture task",
            instruction="Retention fixture.",
            priority=100,
            status=status,
            timeliness_state="overdue",
            phase_code="within_30m",
            activated_at=event.origin_time,
            due_at=event.origin_time + timedelta(minutes=30),
            completed_at=(
                event.origin_time + timedelta(minutes=31)
                if status == "completed"
                else None
            ),
            closed_at=(
                event.origin_time + timedelta(minutes=31)
                if status in {"completed", "not_required", "failed"}
                else None
            ),
            row_version=1,
            created_by="system",
            created_at=event.origin_time,
            updated_at=event.origin_time,
        )
        session.add(task)
        await session.flush()
        return task

    return factory


async def _seed_retention_task(
    session,
    *,
    event_type: str = "test",
    origin_time: datetime,
    status: str,
) -> tuple[WorkgroupTask, EarthquakeEvent, uuid.UUID]:
    event = EarthquakeEvent(
        id=uuid.uuid4(),
        source="retention-concurrency-test",
        canonical_source_id=f"retention-concurrency-{uuid.uuid4().hex}",
        event_type=event_type,
        origin_time=origin_time,
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="retention concurrency fixture",
        geom=WKTElement("POINT(121.5 31.2)", srid=4326),
        lifecycle_state="not_applicable",
    )
    session.add(event)
    await session.flush()
    raw = RawMessage(
        id=uuid.uuid4(),
        source="retention-concurrency-test",
        source_message_id=uuid.uuid4().hex,
        message_kind=event_type,
        checksum=uuid.uuid4().hex,
        payload={"event_id": str(event.id)},
    )
    session.add(raw)
    await session.flush()
    revision = EarthquakeRevision(
        id=uuid.uuid4(),
        event_id=event.id,
        raw_message_id=raw.id,
        revision_no=1,
        revision_kind=event_type,
        origin_time=event.origin_time,
        longitude=event.longitude,
        latitude=event.latitude,
        depth_km=event.depth_km,
        magnitude=event.magnitude,
        place=event.place,
        is_current=True,
        ingested_at=event.origin_time,
    )
    session.add(revision)
    await session.flush()
    task = WorkgroupTask(
        event_id=event.id,
        trigger_revision_id=revision.id,
        task_code=f"retention-concurrency-{uuid.uuid4().hex}",
        source_type="ad_hoc",
        workgroup_code="monitoring_forecast",
        title="Retention concurrency fixture",
        instruction="Retention concurrency fixture.",
        priority=100,
        status=status,
        timeliness_state="overdue",
        phase_code="within_30m",
        activated_at=event.origin_time,
        due_at=event.origin_time + timedelta(minutes=30),
        completed_at=(
            event.origin_time + timedelta(minutes=31)
            if status == "completed"
            else None
        ),
        closed_at=(
            event.origin_time + timedelta(minutes=31)
            if status in {"completed", "not_required", "failed"}
            else None
        ),
        row_version=1,
        created_by="system",
        created_at=event.origin_time,
        updated_at=event.origin_time,
    )
    session.add(task)
    await session.flush()
    return task, event, raw.id


async def _seed_retention_generation_event(
    session,
    *,
    origin_time: datetime,
) -> tuple[EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, uuid.UUID]:
    event = EarthquakeEvent(
        id=uuid.uuid4(),
        source="retention-generation-test",
        canonical_source_id=f"retention-generation-{uuid.uuid4().hex}",
        event_type="test",
        origin_time=origin_time,
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="retention generation fixture",
        geom=WKTElement("POINT(121.5 31.2)", srid=4326),
        lifecycle_state="not_applicable",
    )
    session.add(event)
    await session.flush()
    raw = RawMessage(
        id=uuid.uuid4(),
        source="retention-generation-test",
        source_message_id=uuid.uuid4().hex,
        message_kind="test",
        checksum=uuid.uuid4().hex,
        payload={"event_id": str(event.id)},
    )
    session.add(raw)
    await session.flush()
    revision = EarthquakeRevision(
        id=uuid.uuid4(),
        event_id=event.id,
        raw_message_id=raw.id,
        revision_no=1,
        revision_kind="test",
        origin_time=event.origin_time,
        longitude=event.longitude,
        latitude=event.latitude,
        depth_km=event.depth_km,
        magnitude=event.magnitude,
        place=event.place,
        is_current=True,
        ingested_at=event.origin_time,
    )
    session.add(revision)
    await session.flush()
    outbox = EventLifecycleOutbox(
        id=uuid.uuid4(),
        event_id=event.id,
        revision_id=revision.id,
        trigger_type="collaboration.requested",
        trigger_reason="live",
        payload={
            "event_id": str(event.id),
            "revision_id": str(revision.id),
            "revision_no": 1,
        },
        status="processing",
        attempt_count=1,
        created_at=origin_time,
        available_at=origin_time,
    )
    session.add(outbox)
    await session.flush()
    return event, revision, outbox, raw.id


OBSERVED_AT = datetime(2026, 10, 3, tzinfo=UTC)


async def test_retention_purges_expired_test_and_drill_but_preserves_formal(
    session,
    event_factory,
    task_factory,
):
    test_event = await event_factory(event_type="test", age_days=401)
    drill_event = await event_factory(event_type="drill", age_days=731)
    formal_event = await event_factory(
        event_type="formal",
        age_days=10000,
        lifecycle_state="formal_triggered",
    )
    for event in (test_event, drill_event, formal_event):
        await task_factory(event, status="completed")

    result = await CollaborationRetentionService().purge_due(
        session,
        observed_at=OBSERVED_AT,
    )

    assert result.test_deleted == 1
    assert result.drill_deleted == 1
    assert await session.get(EarthquakeEvent, test_event.id) is None
    assert await session.get(EarthquakeEvent, drill_event.id) is None
    assert await session.get(EarthquakeEvent, formal_event.id) is not None


async def test_retention_purges_test_at_exact_four_hundred_day_boundary(
    session,
    event_factory,
    task_factory,
):
    event = await event_factory(event_type="test", age_days=400)
    await task_factory(event, status="completed")

    result = await CollaborationRetentionService().purge_due(
        session,
        observed_at=OBSERVED_AT,
    )

    assert result.test_deleted == 1
    assert await session.get(EarthquakeEvent, event.id) is None


async def test_retention_uses_calendar_two_year_boundary_for_drill(
    session,
    event_factory,
    task_factory,
):
    observed_at = datetime(2025, 3, 1, tzinfo=UTC)
    exact_anniversary = datetime(2023, 3, 1, tzinfo=UTC)
    one_day_younger = datetime(2023, 3, 2, tzinfo=UTC)
    exact_event = await event_factory(
        event_type="drill",
        origin_time=exact_anniversary,
    )
    younger_event = await event_factory(
        event_type="drill",
        origin_time=one_day_younger,
    )
    await task_factory(exact_event, status="completed")
    await task_factory(younger_event, status="completed")

    result = await CollaborationRetentionService().purge_due(
        session,
        observed_at=observed_at,
    )

    assert result.drill_deleted == 1
    assert await session.get(EarthquakeEvent, exact_event.id) is None
    assert await session.get(EarthquakeEvent, younger_event.id) is not None


async def test_retention_uses_bounded_batches(
    session,
    event_factory,
    task_factory,
):
    events = [
        await event_factory(event_type="test", age_days=401 + index)
        for index in range(3)
    ]
    for event in events:
        await task_factory(event, status="completed")

    result = await CollaborationRetentionService(batch_size=2).purge_due(
        session,
        observed_at=OBSERVED_AT,
    )

    assert result.test_deleted == 3
    assert result.batches >= 2
    for event in events:
        assert await session.get(EarthquakeEvent, event.id) is None


async def test_retention_protects_event_with_active_workgroup_task(
    session,
    event_factory,
    task_factory,
):
    event = await event_factory(event_type="test", age_days=401)
    task = await task_factory(event, status="in_progress")

    result = await CollaborationRetentionService().purge_due(
        session,
        observed_at=OBSERVED_AT,
    )

    assert result.test_deleted == 0
    assert result.protected_active_dependencies == 1
    assert await session.get(EarthquakeEvent, event.id) is not None
    assert await session.get(WorkgroupTask, task.id) is not None


async def test_retention_logs_only_aggregate_counts(
    session,
    event_factory,
    task_factory,
    caplog,
):
    event = await event_factory(event_type="test", age_days=401)
    await task_factory(event, status="completed")

    with caplog.at_level("INFO", logger="app.collaboration.retention"):
        await CollaborationRetentionService().purge_due(
            session,
            observed_at=OBSERVED_AT,
        )

    message = caplog.records[-1].getMessage()
    assert str(event.id) not in message
    assert '"test":1' in message or "'test': 1" in message or "test=1" in message


async def _activate_task(session, task_id: uuid.UUID) -> str:
    async with session.begin():
        await session.execute(
            update(WorkgroupTask)
            .where(WorkgroupTask.id == task_id)
            .values(status="in_progress", updated_at=datetime.now(UTC))
        )
    return "updated"


async def test_retention_locks_existing_tasks_before_dependency_check(
    isolated_session_factory,
):
    origin_time = OBSERVED_AT - timedelta(days=401)
    async with isolated_session_factory() as seed_session:
        async with seed_session.begin():
            task, event, raw_id = await _seed_retention_task(
                seed_session,
                origin_time=origin_time,
                status="completed",
            )
            task_id = task.id
            event_id = event.id

    service = CollaborationRetentionService()
    async with isolated_session_factory() as retention_session:
        async with retention_session.begin():
            assert (
                await service._has_active_dependencies(
                    retention_session,
                    event_id,
                )
                is False
            )

            async def activate():
                async with isolated_session_factory() as activation_session:
                    await _activate_task(activation_session, task_id)

            activation = asyncio.create_task(activate())
            await asyncio.sleep(0.1)
            assert not activation.done()

        await activation

    async with isolated_session_factory() as verify_session:
        stored_task = await verify_session.get(WorkgroupTask, task_id)
        assert stored_task is not None
        assert stored_task.status == "in_progress"

    async with isolated_session_factory() as cleanup_session:
        async with cleanup_session.begin():
            await cleanup_session.execute(
                delete(EarthquakeEvent).where(EarthquakeEvent.id == event_id)
            )
            await cleanup_session.execute(
                delete(RawMessage).where(RawMessage.id == raw_id)
            )


async def test_retention_and_generation_serialize_without_deleting_active_dependency(
    isolated_session_factory,
):
    origin_time = OBSERVED_AT - timedelta(days=401)
    catalog = TaskTemplateCatalog(
        version="retention-generation.1",
        definitions=(
            TaskTemplateDefinition(
                template_code="retention.generation.task",
                workgroup_code=WorkgroupCode.MONITORING_FORECAST,
                phase_code="within_30m",
                title="concurrent generation task",
                source="test",
                applicability={"spatial_class": "any"},
            ),
        ),
    )

    async with isolated_session_factory() as seed_session:
        async with seed_session.begin():
            event, revision, outbox, raw_id = (
                await _seed_retention_generation_event(
                    seed_session,
                    origin_time=origin_time,
                )
            )
            event_id = event.id
            revision_id = revision.id
            outbox_id = outbox.id

    generator = CollaborationTaskGenerator(catalog=catalog)
    retention = CollaborationRetentionService()

    async def generate():
        async with isolated_session_factory() as session:
            async with session.begin():
                await generator.generate_for_revision(
                    session,
                    event_id,
                    revision_id,
                    outbox_id,
                )

    async def retain():
        async with isolated_session_factory() as session:
            async with session.begin():
                return await retention.purge_due(
                    session,
                    observed_at=OBSERVED_AT,
                )

    result, _ = await asyncio.gather(retain(), generate())

    assert result.test_deleted == 0
    async with isolated_session_factory() as verify_session:
        stored_event = await verify_session.get(EarthquakeEvent, event_id)
        tasks = (
            await verify_session.scalars(
                select(WorkgroupTask).where(
                    WorkgroupTask.event_id == event_id,
                    WorkgroupTask.template_version_id.is_not(None),
                )
            )
        ).all()
        assert stored_event is not None
        assert len(tasks) == 1
        assert tasks[0].status == "pending"

    async with isolated_session_factory() as cleanup_session:
        async with cleanup_session.begin():
            await cleanup_session.execute(
                delete(EarthquakeEvent).where(EarthquakeEvent.id == event_id)
            )
            await cleanup_session.execute(
                delete(RawMessage).where(RawMessage.id == raw_id)
            )
