from __future__ import annotations

import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, func, select

from app.artifacts.catalog import EXPECTED_ARTIFACT_KEYS
from app.auth.models import User
from app.collaboration.domain import DeliverableSourceKind, WorkgroupCode
from app.collaboration.generation import CollaborationTaskGenerator
from app.collaboration.models import (
    TaskDeliverable,
    TaskDeliverableVersion,
    WorkgroupTask,
)
from app.collaboration.roster import RosterService
from app.collaboration.service import (
    CollaborationTaskService,
    DeliverableService,
)
from app.command_hall.projector import CommandHallProjector
from app.command_hall.service import CommandHallService
from app.config import settings
from app.db import SessionFactory, engine
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from tests.e2e_fixture import E2E_EVENT_ID, E2E_RAW_MESSAGE_ID, seed_fixture


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


def _p95(samples: list[float]) -> float:
    assert samples
    ordered = sorted(samples)
    index = max(0, int(len(ordered) * 0.95) - 1)
    return ordered[index]


async def _superadmin_actor() -> User:
    async with SessionFactory() as session:
        actor = await session.scalar(
            select(User).where(
                User.username == settings.superadmin_username,
                User.is_active.is_(True),
            )
        )
        if actor is None:
            raise AssertionError("superadmin must be bootstrapped before E2E")
        return actor


async def _linked_codes(event_id: uuid.UUID) -> set[str]:
    async with SessionFactory() as session:
        rows = await session.execute(
            select(
                TaskDeliverable.deliverable_code,
                TaskDeliverableVersion.source_kind,
            )
            .join(
                TaskDeliverableVersion,
                TaskDeliverableVersion.deliverable_id == TaskDeliverable.id,
            )
            .join(WorkgroupTask, WorkgroupTask.id == TaskDeliverable.task_id)
            .where(
                WorkgroupTask.event_id == event_id,
                TaskDeliverableVersion.source_kind
                == DeliverableSourceKind.AUTOMATIC.value,
            )
        )
        return {str(code) for code, _source_kind in rows.all()}


async def _publish_manual_revision(
    event_id: uuid.UUID,
    actor: User,
) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    select(TaskDeliverable, TaskDeliverableVersion)
                    .join(
                        WorkgroupTask,
                        WorkgroupTask.id == TaskDeliverable.task_id,
                    )
                    .join(
                        TaskDeliverableVersion,
                        TaskDeliverableVersion.deliverable_id
                        == TaskDeliverable.id,
                    )
                    .where(
                        WorkgroupTask.event_id == event_id,
                        TaskDeliverable.deliverable_code == "doc.rapid_brief",
                        TaskDeliverableVersion.source_kind
                        == DeliverableSourceKind.AUTOMATIC.value,
                    )
                    .order_by(TaskDeliverableVersion.version_no)
                )
            ).all()
            if not rows:
                raise AssertionError(
                    "the rapid brief automatic deliverable must be linked"
                )
            deliverable = rows[0][0]
            automatic = rows[0][1]
            manual_exists = await session.scalar(
                select(TaskDeliverableVersion.id)
                .where(
                    TaskDeliverableVersion.deliverable_id == deliverable.id,
                    TaskDeliverableVersion.source_kind
                    == DeliverableSourceKind.MANUAL.value,
                )
                .limit(1)
            )

    service = DeliverableService()
    if manual_exists is None:
        async with SessionFactory() as session:
            async with session.begin():
                await service.publish(
                    session,
                    deliverable.id,
                    actor,
                    version_id=automatic.id,
                    publication_note="E2E automatic publication",
                )
        async with SessionFactory() as session:
            async with session.begin():
                manual = await service.add_text_version(
                    session,
                    deliverable.id,
                    actor,
                    text_result={
                        "result": "人工校核后的快速评估简报",
                    },
                    basis_text="E2E manual revision",
                )
                manual_id = manual.id
        async with SessionFactory() as session:
            async with session.begin():
                await service.publish(
                    session,
                    deliverable.id,
                    actor,
                    version_id=manual_id,
                    publication_note="E2E manual publication",
                )


async def _close_all_tasks(event_id: uuid.UUID, actor: User) -> int:
    service = CollaborationTaskService()
    await _publish_required_deliverables(event_id, actor)
    async with SessionFactory() as session:
        async with session.begin():
            tasks = list(
                await session.scalars(
                    select(WorkgroupTask)
                    .where(WorkgroupTask.event_id == event_id)
                    .order_by(WorkgroupTask.created_at, WorkgroupTask.id)
                    .with_for_update()
                )
            )
            for task in tasks:
                if task.status == "completed":
                    continue
                started = await service.start(
                    session,
                    task.id,
                    actor,
                    task.row_version,
                )
                submitted = await service.submit(
                    session,
                    started.id,
                    actor,
                    started.row_version,
                    result_text=f"E2E closure for {task.task_code}",
                )
                await service.complete(
                    session,
                    submitted.id,
                    actor,
                    submitted.row_version,
                )
            return len(tasks)


async def _publish_required_deliverables(
    event_id: uuid.UUID,
    actor: User,
) -> None:
    service = DeliverableService()
    async with SessionFactory() as session:
        deliverable_ids = list(
            await session.scalars(
                select(TaskDeliverable.id)
                .join(WorkgroupTask, WorkgroupTask.id == TaskDeliverable.task_id)
                .where(
                    WorkgroupTask.event_id == event_id,
                    TaskDeliverable.is_required.is_(True),
                )
                .order_by(TaskDeliverable.id)
            )
        )

    for deliverable_id in deliverable_ids:
        async with SessionFactory() as session:
            current = await service.repository.get_current_publication(
                session,
                deliverable_id,
            )
            latest = await service.repository.latest_deliverable_version(
                session,
                deliverable_id,
            )
        if current is not None or latest is None:
            continue
        async with SessionFactory() as session:
            async with session.begin():
                await service.publish(
                    session,
                    deliverable_id,
                    actor,
                    version_id=latest.id,
                    publication_note="E2E required deliverable closure",
                )


async def _seed_formal_events(
    count: int,
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    now = datetime.now(UTC).replace(microsecond=0)
    event_ids: list[uuid.UUID] = []
    raw_ids: list[uuid.UUID] = []
    async with SessionFactory() as session:
        async with session.begin():
            for index in range(count):
                event_id = uuid.uuid4()
                raw_id = uuid.uuid4()
                revision_id = uuid.uuid4()
                source_event_id = f"collaboration-p95-{event_id}"
                raw = RawMessage(
                    id=raw_id,
                    source="cenc-p95",
                    source_message_id=source_event_id,
                    message_kind="formal",
                    provider="cenc",
                    ingest_lane="http",
                    received_at=now,
                    checksum=uuid.uuid4().hex,
                    payload={"fixture": "collaboration-p95"},
                )
                event = EarthquakeEvent(
                    id=event_id,
                    source="cenc-p95",
                    canonical_source_id=f"cenc-p95:{source_event_id}",
                    event_type="formal",
                    origin_time=now + timedelta(milliseconds=index),
                    longitude=Decimal("121.500000"),
                    latitude=Decimal("31.200000"),
                    depth_km=Decimal("10.00"),
                    magnitude=Decimal("5.2"),
                    place=f"上海协同性能验收事件 {index}",
                    geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                    t1_at=now,
                    lifecycle_state="formal_triggered",
                    institutional_level="三级",
                    service_level=3,
                )
                session.add_all([raw, event])
                await session.flush()
                revision = EarthquakeRevision(
                    id=revision_id,
                    event_id=event.id,
                    raw_message_id=raw.id,
                    revision_no=1,
                    revision_kind="formal",
                    source_event_id=source_event_id,
                    origin_time=event.origin_time,
                    longitude=event.longitude,
                    latitude=event.latitude,
                    depth_km=event.depth_km,
                    magnitude=event.magnitude,
                    place=event.place,
                    ingested_at=now,
                    is_current=True,
                    provider="cenc",
                    ingest_lane="http",
                    inside_shanghai=True,
                    distance_to_boundary_km=Decimal("0"),
                )
                session.add(revision)
                await session.flush()
                event.current_revision_id = revision.id
                session.add(
                    EventLifecycleOutbox(
                        event_id=event.id,
                        revision_id=revision.id,
                        trigger_type="collaboration.requested",
                        trigger_reason="live",
                        payload={
                            "event_id": str(event.id),
                            "revision_id": str(revision.id),
                            "revision_no": 1,
                            "intensity_threshold": "2.0",
                        },
                        status="pending",
                        created_at=now,
                        available_at=now,
                    )
                )
                await RosterService().snapshot_for_event(session, event.id)
                event_ids.append(event.id)
                raw_ids.append(raw.id)
    return event_ids, raw_ids


async def _cleanup_events(
    event_ids: list[uuid.UUID],
    raw_ids: list[uuid.UUID],
) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            if event_ids:
                task_ids = select(WorkgroupTask.id).where(
                    WorkgroupTask.event_id.in_(event_ids)
                )
                deliverable_ids = select(TaskDeliverable.id).where(
                    TaskDeliverable.task_id.in_(task_ids)
                )
                await session.execute(
                    delete(TaskDeliverableVersion).where(
                        TaskDeliverableVersion.deliverable_id.in_(
                            deliverable_ids
                        )
                    )
                )
                await session.execute(
                    delete(TaskDeliverable).where(
                        TaskDeliverable.task_id.in_(task_ids)
                    )
                )
                await session.execute(
                    delete(WorkgroupTask).where(
                        WorkgroupTask.event_id.in_(event_ids)
                    )
                )
                await session.execute(
                    delete(EarthquakeEvent).where(
                        EarthquakeEvent.id.in_(event_ids)
                    )
                )
            if raw_ids:
                await session.execute(
                    delete(RawMessage).where(RawMessage.id.in_(raw_ids))
                )


async def test_formal_event_closes_all_seven_groups_with_dual_versions() -> None:
    try:
        seeded = await seed_fixture(preserve_data_assets=False)
        assert seeded["complete_count"] == 28
        assert seeded["degraded_count"] == 11
        assert seeded["failed_count"] == 0
        assert seeded["timeout_count"] == 0

        actor = await _superadmin_actor()
        async with SessionFactory() as session:
            task_rows = list(
                await session.scalars(
                    select(WorkgroupTask).where(
                        WorkgroupTask.event_id == E2E_EVENT_ID
                    )
                )
            )
        assert {task.workgroup_code for task in task_rows} == {
            group.value for group in WorkgroupCode
        }
        assert len(task_rows) == 60

        linked_codes = await _linked_codes(E2E_EVENT_ID)
        assert len(EXPECTED_ARTIFACT_KEYS[:27]) == 27
        assert set(EXPECTED_ARTIFACT_KEYS[:27]) <= linked_codes
        assert set(EXPECTED_ARTIFACT_KEYS[27:]) <= linked_codes

        await _publish_manual_revision(E2E_EVENT_ID, actor)
        completed = await _close_all_tasks(E2E_EVENT_ID, actor)
        assert completed == 60

        async with SessionFactory() as session:
            async with session.begin():
                await CommandHallProjector().refresh_event(
                    session,
                    E2E_EVENT_ID,
                )
            overview = await CommandHallService().overview(
                session,
                E2E_EVENT_ID,
            )

        counts = overview.task_counts
        assert counts.completed == len(task_rows)
        assert counts.overdue == 0
        assert counts.failed == 0
        assert overview.dual_version_count >= 1
    finally:
        await _cleanup_events([E2E_EVENT_ID], [E2E_RAW_MESSAGE_ID])


async def test_formal_task_generation_and_overview_meet_p95_targets() -> None:
    count = 20
    event_ids, raw_ids = await _seed_formal_events(count)
    generation_samples: list[float] = []
    try:
        generator = CollaborationTaskGenerator(
            catalog_path=settings.collaboration_task_template_path,
        )
        async with SessionFactory() as session:
            rows = (
                await session.execute(
                    select(
                        EarthquakeEvent.id,
                        EarthquakeEvent.current_revision_id,
                        EventLifecycleOutbox.id,
                    )
                    .join(
                        EventLifecycleOutbox,
                        EventLifecycleOutbox.event_id == EarthquakeEvent.id,
                    )
                    .where(EarthquakeEvent.id.in_(event_ids))
                )
            ).all()
        generation_inputs = sorted(
            (
                (event_id, revision_id, outbox_id)
                for event_id, revision_id, outbox_id in rows
            ),
            key=lambda item: str(item[0]),
        )
        assert len(generation_inputs) == count
        for event_id, revision_id, outbox_id in generation_inputs:
            started = time.perf_counter()
            async with SessionFactory() as session:
                async with session.begin():
                    await generator.generate_for_revision(
                        session,
                        event_id,
                        revision_id,
                        outbox_id,
                    )
            generation_samples.append(time.perf_counter() - started)

        async with SessionFactory() as session:
            task_counts = dict(
                (
                    await session.execute(
                        select(
                            WorkgroupTask.event_id,
                            func.count(WorkgroupTask.id),
                        )
                        .where(WorkgroupTask.event_id.in_(event_ids))
                        .group_by(WorkgroupTask.event_id)
                    )
                ).all()
            )
        assert task_counts == {event_id: 60 for event_id in event_ids}

        overview_samples: list[float] = []
        service = CommandHallService()
        for event_id in event_ids:
            async with SessionFactory() as session:
                async with session.begin():
                    await CommandHallProjector().refresh_event(
                        session,
                        event_id,
                    )
            for _ in range(3):
                started = time.perf_counter()
                async with SessionFactory() as session:
                    overview = await service.overview(session, event_id)
                overview_samples.append(time.perf_counter() - started)
                assert overview.group_count == 7

        generation_p95 = _p95(generation_samples)
        overview_p95 = _p95(overview_samples)
        assert generation_p95 <= 5.0
        assert overview_p95 <= 0.5
        assert statistics.median(generation_samples) <= 5.0
        assert statistics.median(overview_samples) <= 0.5
    finally:
        await _cleanup_events(event_ids, raw_ids)
