import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.command_hall.models import (
    CommandHallAlertProjection,
    CommandHallEventProjection,
    CommandHallGroupProjection,
)
from app.command_hall.projector import CommandHallProjector
from app.command_hall.service import CommandHallService
from app.collaboration.domain import WorkgroupCode
from app.collaboration.models import (
    CollaborationOutbox,
    WorkgroupTask,
)
from app.collaboration.worker import (
    ProjectionOutboxDispatcher,
    run_worker_cycle,
)
from app.config import settings
from app.artifacts.models import (
    ArtifactPublication,
    GeneratedArtifact,
    ProductionRun,
    ProductionTask,
)
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage


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
async def projection_session_factory():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest.fixture
def event_factory(session):
    async def factory(
        *,
        event_type: str = "formal",
        lifecycle_state: str = "formal_triggered",
        origin_time: datetime | None = None,
    ) -> EarthquakeEvent:
        event = EarthquakeEvent(
            id=uuid.uuid4(),
            source="command-hall-test",
            canonical_source_id=f"hall-{uuid.uuid4().hex}",
            event_type=event_type,
            origin_time=origin_time or datetime(2026, 10, 3, 0, 0, tzinfo=UTC),
            longitude=Decimal("121.500000"),
            latitude=Decimal("31.200000"),
            depth_km=Decimal("10.00"),
            magnitude=Decimal("5.2"),
            place="command hall projection fixture",
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
            source="command-hall-test",
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
def task_factory(session, event_factory, revision_factory):
    async def factory(
        event: EarthquakeEvent,
        *,
        workgroup_code: str = WorkgroupCode.MONITORING_FORECAST.value,
        status: str = "pending",
        timeliness_state: str = "on_time",
        due_at: datetime | None = None,
    ) -> WorkgroupTask:
        revision = await session.scalar(
            select(EarthquakeRevision)
            .where(EarthquakeRevision.event_id == event.id)
            .order_by(EarthquakeRevision.revision_no)
            .limit(1)
        )
        if revision is None:
            revision = await revision_factory(event)
        now = datetime.now(UTC)
        task = WorkgroupTask(
            event_id=event.id,
            trigger_revision_id=revision.id,
            task_code=f"task-{uuid.uuid4().hex}",
            source_type="ad_hoc",
            workgroup_code=workgroup_code,
            title="Projection task",
            instruction="Exercise the projection.",
            priority=100,
            status=status,
            timeliness_state=timeliness_state,
            phase_code="within_30m",
            activated_at=now,
            due_at=due_at or now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.flush()
        return task

    return factory


@pytest.fixture
async def collaboration_fixture(session, event_factory, revision_factory, task_factory):
    event = await event_factory()
    await revision_factory(event)
    await task_factory(
        event,
        workgroup_code=WorkgroupCode.NEWS_INFORMATION.value,
        status="pending_review",
    )
    await task_factory(
        event,
        workgroup_code=WorkgroupCode.MONITORING_FORECAST.value,
        status="in_progress",
        timeliness_state="overdue",
        due_at=datetime.now(UTC) - timedelta(hours=1),
    )
    return SimpleProjectionFixture(event=event)


class SimpleProjectionFixture:
    def __init__(self, event: EarthquakeEvent) -> None:
        self.event = event


async def test_projection_counts_overdue_and_waiting_for_review(
    session, collaboration_fixture
):
    await CommandHallProjector().refresh_event(
        session, collaboration_fixture.event.id
    )
    overview = await CommandHallService().overview(
        session, collaboration_fixture.event.id
    )

    assert overview.group_count == 7
    assert overview.task_counts.pending_review == 1
    assert overview.task_counts.overdue == 1
    assert overview.projection_version >= 1


async def test_projection_upserts_one_event_and_seven_current_groups(
    session, collaboration_fixture
):
    projector = CommandHallProjector()
    result = await projector.refresh_event(session, collaboration_fixture.event.id)

    assert result.event_id == collaboration_fixture.event.id
    assert result.projection_version == 1
    assert result.group_projection_count == 7
    assert (
        await session.scalar(
            select(func.count())
            .select_from(CommandHallEventProjection)
            .where(
                CommandHallEventProjection.event_id
                == collaboration_fixture.event.id
            )
        )
        == 1
    )
    assert (
        await session.scalar(
            select(func.count())
            .select_from(CommandHallGroupProjection)
            .where(
                CommandHallGroupProjection.event_id
                == collaboration_fixture.event.id,
                CommandHallGroupProjection.is_current.is_(True),
            )
        )
        == 7
    )


async def test_projection_replay_is_idempotent_and_increments_once(
    session, collaboration_fixture
):
    projector = CommandHallProjector()
    first = await projector.refresh_event(session, collaboration_fixture.event.id)
    second = await projector.refresh_event(session, collaboration_fixture.event.id)

    assert second.projection_version == first.projection_version + 1
    assert (
        await session.scalar(
            select(func.count())
            .select_from(CommandHallEventProjection)
            .where(
                CommandHallEventProjection.event_id
                == collaboration_fixture.event.id
            )
        )
        == 1
    )
    assert (
        await session.scalar(
            select(func.count())
            .select_from(CommandHallGroupProjection)
            .where(
                CommandHallGroupProjection.event_id
                == collaboration_fixture.event.id,
                CommandHallGroupProjection.is_current.is_(True),
            )
        )
        == 7
    )
    assert (
        await session.scalar(
            select(func.count())
            .select_from(CommandHallGroupProjection)
            .where(
                CommandHallGroupProjection.event_id
                == collaboration_fixture.event.id,
            )
        )
        == 14
    )
    alert_count = await session.scalar(
        select(func.count())
        .select_from(CommandHallAlertProjection)
        .where(
            CommandHallAlertProjection.event_id
            == collaboration_fixture.event.id
        )
    )
    assert alert_count >= 1


async def test_projection_recreates_derived_alerts(
    session, collaboration_fixture
):
    projector = CommandHallProjector()
    await projector.refresh_event(session, collaboration_fixture.event.id)
    first_ids = set(
        await session.scalars(
            select(CommandHallAlertProjection.id).where(
                CommandHallAlertProjection.event_id
                == collaboration_fixture.event.id
            )
        )
    )
    await projector.refresh_event(session, collaboration_fixture.event.id)
    second_ids = set(
        await session.scalars(
            select(CommandHallAlertProjection.id).where(
                CommandHallAlertProjection.event_id
                == collaboration_fixture.event.id
            )
        )
    )

    assert first_ids
    assert len(first_ids) == len(second_ids)
    assert first_ids.isdisjoint(second_ids)


async def test_artifact_summary_exposes_publication_source_fields(
    session,
    event_factory,
    revision_factory,
):
    event = await event_factory()
    revision = await revision_factory(event)
    now = datetime.now(UTC)
    run = ProductionRun(
        event_id=event.id,
        revision_id=revision.id,
        revision_no=revision.revision_no,
        production_mode="live",
        launch_mode="standalone",
        status="completed",
        priority=100,
        deadline_basis_at=now,
        deadline_at=now + timedelta(minutes=5),
        deadline_kind="event_deadline",
        catalog_version="test",
        generation_seq=1,
        generation_scope="artifact:map.epicenter",
        required_outputs=[],
        is_current=True,
        completed_at=now,
    )
    session.add(run)
    await session.flush()
    task = ProductionTask(
        production_run_id=run.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        kind="map",
        priority=100,
        sequence=1,
        status="succeeded",
        deadline_at=now + timedelta(minutes=5),
        completed_at=now,
    )
    session.add(task)
    await session.flush()
    artifact = GeneratedArtifact(
        production_run_id=run.id,
        production_task_id=task.id,
        event_id=event.id,
        revision_id=revision.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        artifact_version=1,
        is_final=True,
        production_mode="live",
        status="complete",
        quality_grade="A",
        publication_mode="automatic",
        file_name="epicenter.png",
        format="png",
        storage_path="objects/epicenter.png",
        checksum="a" * 64,
        size_bytes=1024,
        generated_at=now,
    )
    session.add(artifact)
    await session.flush()
    session.add(
        ArtifactPublication(
            event_id=event.id,
            revision_id=revision.id,
            revision_no=revision.revision_no,
            production_mode="live",
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            artifact_id=artifact.id,
            production_run_id=run.id,
            generation_seq=1,
            published_by="system",
            published_at=now,
            is_forced=False,
        )
    )
    await session.flush()
    task.final_artifact_id = artifact.id

    await CommandHallProjector().refresh_event(session, event.id)
    overview = await CommandHallService().overview(session, event.id)
    summary = overview.artifact_summary["latest_artifacts"][0]

    assert summary["production_mode"] == "live"
    assert summary["publication_mode"] == "automatic"
    assert summary["is_forced"] is False


async def test_overview_marks_projection_lag_only_after_five_seconds(
    session,
    collaboration_fixture,
):
    projector = CommandHallProjector()
    service = CommandHallService()
    event = collaboration_fixture.event
    await projector.refresh_event(session, event.id)
    observed_at = datetime.now(UTC)

    session.add(
        CommandHallAlertProjection(
            event_id=event.id,
            workgroup_code=None,
            task_id=None,
            alert_key="projection.lag",
            alert_type="projection.lag",
            severity="warning",
            status="open",
            title="数据同步中",
            detail={
                "projection_lag_seconds": 5.25,
                "projection_version": 1,
                "source_updated_at": (
                    observed_at - timedelta(seconds=5.25)
                ).isoformat(),
            },
            projection_version=1,
            first_seen_at=observed_at - timedelta(seconds=5.25),
            updated_at=observed_at - timedelta(seconds=5.25),
        )
    )
    await session.flush()

    lagged = await service.overview(session, event.id)

    assert lagged.sync_status == "syncing"
    assert lagged.projection_lag_seconds == pytest.approx(5.25)
    assert any(
        alert["alert_type"] == "projection.lag"
        for alert in lagged.alerts
    )

    await session.execute(
        delete(CommandHallAlertProjection).where(
            CommandHallAlertProjection.event_id == event.id,
            CommandHallAlertProjection.alert_key == "projection.lag",
        )
    )
    await session.flush()

    current = await service.overview(session, event.id)

    assert current.sync_status == "current"
    assert current.projection_lag_seconds == 0
    assert all(
        alert["alert_type"] != "projection.lag"
        for alert in current.alerts
    )


async def test_projection_dispatcher_persists_lag_alert_only_after_five_seconds(
    projection_session_factory,
):
    observed_at = datetime.now(UTC)
    lagged_event_id = uuid.uuid4()
    current_event_id = uuid.uuid4()

    async with projection_session_factory() as session:
        async with session.begin():
            for event_id in (lagged_event_id, current_event_id):
                session.add(
                    EarthquakeEvent(
                        id=event_id,
                        source="command-hall-test",
                        canonical_source_id=f"lag-{event_id}",
                        event_type="formal",
                        origin_time=observed_at - timedelta(minutes=1),
                        longitude=Decimal("121.500000"),
                        latitude=Decimal("31.200000"),
                        depth_km=Decimal("10.00"),
                        magnitude=Decimal("5.2"),
                        place="projection lag fixture",
                        geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                        lifecycle_state="formal_triggered",
                    )
                )
                await session.flush()
                session.add(
                    CommandHallEventProjection(
                        event_id=event_id,
                        event_snapshot={},
                        task_counts={},
                        artifact_summary={},
                        alert_summary={},
                        projection_version=1,
                        updated_at=observed_at - timedelta(seconds=10),
                    )
                )
            await session.flush()
            session.add(
                CollaborationOutbox(
                    event_id=lagged_event_id,
                    task_id=None,
                    event_type="projection.test",
                    payload={},
                    status="pending",
                    attempt_count=0,
                    available_at=observed_at,
                    created_at=observed_at - timedelta(seconds=5.25),
                    updated_at=observed_at - timedelta(seconds=5.25),
                )
            )
            session.add(
                CollaborationOutbox(
                    event_id=current_event_id,
                    task_id=None,
                    event_type="projection.test",
                    payload={},
                    status="pending",
                    attempt_count=0,
                    available_at=observed_at,
                    created_at=observed_at - timedelta(seconds=5),
                    updated_at=observed_at - timedelta(seconds=5),
                )
            )

    async def no_op_handler(_session, _outbox) -> None:
        return None

    dispatcher = ProjectionOutboxDispatcher(
        projection_session_factory,
        handler=no_op_handler,
        now=lambda: observed_at,
    )
    try:
        assert await dispatcher.dispatch_once() == 2

        async with projection_session_factory() as session:
            lagged_alert = await session.scalar(
                select(CommandHallAlertProjection).where(
                    CommandHallAlertProjection.event_id == lagged_event_id,
                    CommandHallAlertProjection.alert_key
                    == "projection.lag",
                )
            )
            current_alert = await session.scalar(
                select(CommandHallAlertProjection).where(
                    CommandHallAlertProjection.event_id == current_event_id,
                    CommandHallAlertProjection.alert_key
                    == "projection.lag",
                )
            )

        assert lagged_alert is not None
        assert lagged_alert.detail["projection_lag_seconds"] == pytest.approx(
            5.25
        )
        assert current_alert is None
    finally:
        async with projection_session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(EarthquakeEvent).where(
                        EarthquakeEvent.id.in_(
                            (lagged_event_id, current_event_id)
                        )
                    )
                )


async def test_worker_default_dispatches_projection_outbox(
    projection_session_factory,
):
    event_id = uuid.uuid4()
    now = datetime.now(UTC)

    class _FakeCollaborationDispatcher:
        async def dispatch_once(self) -> int:
            return 1

    class _FakeScheduler:
        async def run_once(self, session, observed_at):
            del session, observed_at
            return SimpleNamespace(
                tasks_scanned=0,
                timeliness_updated=0,
                assigned_created=0,
                due_soon_created=0,
                overdue_marked=0,
                overdue_reminder_count=0,
            )

    class _FakeNotificationService:
        async def dispatch_pending(self, session, limit):
            del session, limit
            return SimpleNamespace(
                processed=0,
                sent=0,
                retried=0,
                failed=0,
                fallback_sent=0,
            )

    async with projection_session_factory() as seed_session:
        async with seed_session.begin():
            seed_session.add(
                EarthquakeEvent(
                    id=event_id,
                    source="command-hall-worker-test",
                    canonical_source_id=f"hall-worker-{event_id}",
                    event_type="formal",
                    origin_time=now,
                    longitude=Decimal("121.500000"),
                    latitude=Decimal("31.200000"),
                    depth_km=Decimal("10.00"),
                    magnitude=Decimal("5.2"),
                    place="command hall worker fixture",
                    geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                    lifecycle_state="formal_triggered",
                )
            )
            await seed_session.flush()
            seed_session.add(
                CollaborationOutbox(
                    event_id=event_id,
                    task_id=None,
                    event_type="artifact_linked",
                    payload={"event_id": str(event_id)},
                    status="pending",
                    attempt_count=0,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )

    try:
        result = await run_worker_cycle(
            projection_session_factory,
            observed_at=now,
            collaboration_dispatcher=_FakeCollaborationDispatcher(),
            scheduler=_FakeScheduler(),
            notification_service=_FakeNotificationService(),
        )

        assert result.projection_dispatched == 1
        async with projection_session_factory() as check_session:
            projection_count = await check_session.scalar(
                select(func.count())
                .select_from(CommandHallEventProjection)
                .where(CommandHallEventProjection.event_id == event_id)
            )
            assert projection_count == 1
    finally:
        async with projection_session_factory() as cleanup_session:
            async with cleanup_session.begin():
                await cleanup_session.execute(
                    delete(EarthquakeEvent).where(
                        EarthquakeEvent.id == event_id
                    )
                )


async def test_active_event_orders_formal_drill_test_then_other(
    session, event_factory, revision_factory
):
    projector = CommandHallProjector()
    service = CommandHallService()

    other = await event_factory(
        event_type="auto",
        lifecycle_state="not_applicable",
        origin_time=datetime(2026, 10, 3, 4, 0, tzinfo=UTC),
    )
    test = await event_factory(
        event_type="test",
        lifecycle_state="active",
        origin_time=datetime(2026, 10, 3, 3, 0, tzinfo=UTC),
    )
    drill = await event_factory(
        event_type="drill",
        lifecycle_state="active",
        origin_time=datetime(2026, 10, 3, 2, 0, tzinfo=UTC),
    )
    formal = await event_factory(
        event_type="formal",
        lifecycle_state="formal_triggered",
        origin_time=datetime(2026, 10, 3, 1, 0, tzinfo=UTC),
    )
    for event in (other, test, drill, formal):
        await revision_factory(event)
        await projector.refresh_event(session, event.id)

    assert await service.active_event(session) == formal.id

    formal.lifecycle_state = "not_applicable"
    await projector.refresh_event(session, formal.id)
    assert await service.active_event(session) == drill.id

    drill.lifecycle_state = "not_applicable"
    await projector.refresh_event(session, drill.id)
    assert await service.active_event(session) == test.id

    test.lifecycle_state = "not_applicable"
    await projector.refresh_event(session, test.id)
    assert await service.active_event(session) == other.id


async def test_overview_group_and_task_read_contracts(
    session, collaboration_fixture
):
    projector = CommandHallProjector()
    service = CommandHallService()
    event = collaboration_fixture.event
    await projector.refresh_event(session, event.id)

    overview = await service.overview(session, event.id)
    group = await service.group_detail(
        session,
        event.id,
        WorkgroupCode.MONITORING_FORECAST.value,
    )
    task = await session.scalar(
        select(WorkgroupTask)
        .where(
            WorkgroupTask.event_id == event.id,
            WorkgroupTask.workgroup_code
            == WorkgroupCode.MONITORING_FORECAST.value,
        )
        .limit(1)
    )
    detail = await service.task_detail(session, task.id)

    assert overview.event_id == event.id
    assert overview.event["event_type"] == "formal"
    assert overview.task_counts.total == 2
    assert overview.task_counts.overdue == 1
    assert overview.task_counts.pending_review == 1
    assert overview.dual_version_count == 0
    assert group.event_id == event.id
    assert group.group["workgroup_code"] == "monitoring_forecast"
    assert group.tasks
    assert detail.id == task.id
    assert detail.event_id == event.id
    assert detail.task["workgroup_code"] == "monitoring_forecast"
