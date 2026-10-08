import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.collaboration.domain import WorkgroupCode
from app.collaboration.generation import (
    CollaborationOutboxDispatcher,
)
from app.collaboration.models import (
    CollaborationSettings,
    CollaborationTaskEvent,
    CollaborationTaskTemplateVersion,
    WorkgroupRosterSnapshot,
    WorkgroupTask,
)
from app.config import settings
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.events.repository import EventRepository
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.regions.domain import RegionContext


CATALOG_PATH = "config/collaboration/shanghai-2026-tasks.yaml"
BASE_RECEIVED_AT = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)


@pytest.fixture
async def session():
    local_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(local_engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await local_engine.dispose()


async def _delete_generation_data(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(CollaborationTaskEvent))
            await session.execute(delete(WorkgroupTask))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(WorkgroupRosterSnapshot))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
            await session.execute(
                delete(CollaborationSettings).where(
                    CollaborationSettings.id
                    == uuid.UUID("00000000-0000-0000-0000-000000000018")
                )
            )
            session.add(
                CollaborationSettings(
                    id=uuid.UUID("00000000-0000-0000-0000-000000000018"),
                    intensity_threshold=Decimal("2.0"),
                    row_version=1,
                )
            )


@pytest.fixture(autouse=True)
async def clean_generation_data(session_factory):
    await engine.dispose()
    await _delete_generation_data(session_factory)
    yield
    await _delete_generation_data(session_factory)
    await engine.dispose()


async def _ingest(
    session_factory,
    *,
    kind: EventKind,
    magnitude: str,
    inside_shanghai: bool,
    distance_to_boundary_km: str | None,
    max_intensity: str | None,
    suffix: str,
    received_at: datetime = BASE_RECEIVED_AT,
    report_number: int | None = None,
    task_template_catalog_path: str | None = None,
) -> tuple[EarthquakeEvent, EarthquakeRevision]:
    source_event_id = f"COLLAB-GENERATION-{suffix}"
    event = NormalizedEvent(
        kind=kind,
        source="cenc",
        source_event_id=source_event_id,
        origin_time=datetime(2026, 10, 3, 0, 55, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal(magnitude),
        place="generation test event",
        report_time=received_at,
        report_number=report_number,
    )
    outcome = await EventService(
        session_factory,
        repository=(
            EventRepository(
                session_factory,
                task_template_catalog_path=task_template_catalog_path,
            )
            if task_template_catalog_path is not None
            else None
        ),
    ).ingest_collected(
        raw_payload={
            "EventID": source_event_id,
            "type": kind.value,
            "magnitude": magnitude,
            "receivedAt": received_at.isoformat(),
        },
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received_at,
        response_input=ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=inside_shanghai,
            distance_to_boundary_km=(
                Decimal(distance_to_boundary_km)
                if distance_to_boundary_km is not None
                else None
            ),
            deaths=None,
            max_intensity=(
                Decimal(max_intensity) if max_intensity is not None else None
            ),
        ),
        region_context=RegionContext(
            inside_shanghai=inside_shanghai,
            distance_to_boundary_km=(
                Decimal(distance_to_boundary_km)
                if distance_to_boundary_km is not None
                else None
            ),
            boundary_version="generation-boundary.1",
            computed_at=received_at,
        ),
    )
    async with session_factory() as session:
        persisted_event = await session.get(
            EarthquakeEvent,
            uuid.UUID(outcome.event_id),
        )
        persisted_revision = await session.get(
            EarthquakeRevision,
            uuid.UUID(outcome.revision_id),
        )
    assert persisted_event is not None
    assert persisted_revision is not None
    return persisted_event, persisted_revision


@pytest.fixture
def formal_event_factory(session_factory):
    async def factory() -> tuple[EarthquakeEvent, EarthquakeRevision]:
        return await _ingest(
            session_factory,
            kind=EventKind.FORMAL,
            magnitude="5.2",
            inside_shanghai=True,
            distance_to_boundary_km="0",
            max_intensity="6",
            suffix=uuid.uuid4().hex,
            report_number=1,
        )

    return factory


@pytest.fixture
def out_of_scope_event_factory(session_factory):
    async def factory() -> tuple[EarthquakeEvent, EarthquakeRevision]:
        return await _ingest(
            session_factory,
            kind=EventKind.FORMAL,
            magnitude="5.2",
            inside_shanghai=False,
            distance_to_boundary_km="50",
            max_intensity="6",
            suffix=uuid.uuid4().hex,
            report_number=1,
        )

    return factory


async def test_formal_revision_generates_seven_group_tasks_once(
    session,
    formal_event_factory,
    session_factory,
):
    event, revision = await formal_event_factory()
    dispatcher = CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=CATALOG_PATH,
    )

    assert await dispatcher.dispatch_once() == 1
    assert await dispatcher.dispatch_once() == 0

    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    assert {task.workgroup_code for task in tasks} == {
        group.value for group in WorkgroupCode
    }
    assert len(tasks) == 60
    assert len(tasks) == len({task.task_code for task in tasks})
    version_ids_by_code = dict(
        (
            await session.execute(
                select(
                    CollaborationTaskTemplateVersion.template_code,
                    CollaborationTaskTemplateVersion.id,
                ).where(
                    CollaborationTaskTemplateVersion.version
                    == "shanghai-2026.1"
                )
            )
        ).all()
    )
    assert {
        task.task_code: task.template_version_id for task in tasks
    } == {
        task.task_code: version_ids_by_code[task.task_code]
        for task in tasks
    }

    snapshots = (
        await session.scalars(
            select(WorkgroupRosterSnapshot).where(
                WorkgroupRosterSnapshot.event_id == event.id
            )
        )
    ).all()
    assert {snapshot.workgroup_code for snapshot in snapshots} == {
        group.value for group in WorkgroupCode
    }

    outbox = await session.scalar(
        select(EventLifecycleOutbox).where(
            EventLifecycleOutbox.event_id == event.id,
            EventLifecycleOutbox.revision_id == revision.id,
            EventLifecycleOutbox.trigger_type == "collaboration.requested",
        )
    )
    assert outbox is not None
    assert outbox.status == "published"
    assert outbox.payload["intensity_threshold"] == "2.0"
    assert outbox.payload["policy_version"] == 1
    assert (
        outbox.payload["region_boundary_version"]
        == revision.region_boundary_version
        == "generation-boundary.1"
    )


async def test_out_of_scope_event_creates_only_record_notify_task(
    session,
    out_of_scope_event_factory,
    session_factory,
):
    event, _revision = await out_of_scope_event_factory()

    await CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=CATALOG_PATH,
    ).dispatch_once()

    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    assert [task.task_code for task in tasks] == [
        "news.external_event_record_notify"
    ]
    assert tasks[0].due_at == event.origin_time + timedelta(seconds=300)


async def test_auto_event_does_not_enqueue_or_generate_collaboration(
    session,
    session_factory,
):
    event, _revision = await _ingest(
        session_factory,
        kind=EventKind.AUTO,
        magnitude="5.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="6",
        suffix=uuid.uuid4().hex,
    )

    outbox_types = (
        await session.scalars(
            select(EventLifecycleOutbox.trigger_type).where(
                EventLifecycleOutbox.event_id == event.id
            )
        )
    ).all()
    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()

    assert "collaboration.requested" not in outbox_types
    assert tasks == []


async def test_pending_outbox_uses_frozen_policy_after_settings_change(
    session,
    session_factory,
    tmp_path,
):
    catalog_path = tmp_path / "policy.yaml"
    catalog_path.write_text(
        """
version: generation-policy.1
tasks:
  - code: generation.policy_two
    group: emergency_technology
    phase: within_30m
    title: threshold two
    source: test
    applicability:
      spatial_class: any
      minimum_max_intensity: 2.0
  - code: generation.policy_four
    group: emergency_technology
    phase: within_30m
    title: threshold four
    source: test
    applicability:
      spatial_class: any
      minimum_max_intensity: 4.0
""".strip(),
        encoding="utf-8",
    )
    event, _revision = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="4.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="3",
        suffix=uuid.uuid4().hex,
        report_number=1,
        task_template_catalog_path=str(catalog_path),
    )

    async with session_factory() as write_session:
        async with write_session.begin():
            policy = await write_session.get(
                CollaborationSettings,
                uuid.UUID("00000000-0000-0000-0000-000000000018"),
                with_for_update=True,
            )
            assert policy is not None
            policy.intensity_threshold = Decimal("5.0")
            policy.row_version = 99

    await CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=str(catalog_path),
    ).dispatch_once()

    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    assert [task.task_code for task in tasks] == [
        "generation.policy_two",
        "generation.policy_four",
    ]


async def test_official_catalog_uses_frozen_threshold_after_settings_change(
    session,
    session_factory,
):
    async with session_factory() as write_session:
        async with write_session.begin():
            policy = await write_session.get(
                CollaborationSettings,
                uuid.UUID("00000000-0000-0000-0000-000000000018"),
                with_for_update=True,
            )
            assert policy is not None
            policy.intensity_threshold = Decimal("5.0")
            policy.row_version = 2

    event, _revision = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="4.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="3",
        suffix=uuid.uuid4().hex,
        report_number=1,
    )

    async with session_factory() as write_session:
        async with write_session.begin():
            policy = await write_session.get(
                CollaborationSettings,
                uuid.UUID("00000000-0000-0000-0000-000000000018"),
                with_for_update=True,
            )
            assert policy is not None
            policy.intensity_threshold = Decimal("1.0")
            policy.row_version = 3

    await CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=CATALOG_PATH,
    ).dispatch_once()

    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    assert [task.task_code for task in tasks] == [
        "news.external_event_record_notify"
    ]

    outbox = await session.scalar(
        select(EventLifecycleOutbox).where(
            EventLifecycleOutbox.event_id == event.id,
            EventLifecycleOutbox.trigger_type == "collaboration.requested",
        )
    )
    assert outbox is not None
    assert outbox.payload["intensity_threshold"] == "5.0"
    assert outbox.payload["policy_version"] == 2


async def test_pending_event_uses_template_version_frozen_at_ingest(
    session,
    session_factory,
    tmp_path,
):
    catalog_path = tmp_path / "frozen-at-ingest.yaml"
    catalog_path.write_text(
        """
version: generation-frozen.1
tasks:
  - code: generation.frozen_v1
    group: emergency_technology
    phase: within_30m
    title: frozen v1
    source: test
""".strip(),
        encoding="utf-8",
    )
    event, revision = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="5.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="6",
        suffix=uuid.uuid4().hex,
        report_number=1,
        task_template_catalog_path=str(catalog_path),
    )

    async with session_factory() as write_session:
        outbox = await write_session.scalar(
            select(EventLifecycleOutbox).where(
                EventLifecycleOutbox.event_id == event.id,
                EventLifecycleOutbox.revision_id == revision.id,
                EventLifecycleOutbox.trigger_type == "collaboration.requested",
            )
        )
    assert outbox is not None
    assert outbox.status == "pending"
    assert (
        outbox.payload.get("template_catalog", {}).get("version")
        == "generation-frozen.1"
    )

    catalog_path.write_text(
        """
version: generation-frozen.2
tasks:
  - code: generation.frozen_v2
    group: emergency_technology
    phase: within_30m
    title: frozen v2
    source: test
""".strip(),
        encoding="utf-8",
    )

    assert await CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=str(catalog_path),
    ).dispatch_once() == 1

    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    assert [task.task_code for task in tasks] == ["generation.frozen_v1"]
    v1 = await session.scalar(
        select(CollaborationTaskTemplateVersion).where(
            CollaborationTaskTemplateVersion.version == "generation-frozen.1",
            CollaborationTaskTemplateVersion.template_code
            == "generation.frozen_v1",
        )
    )
    assert v1 is not None
    assert tasks[0].template_version_id == v1.id


@pytest.mark.parametrize(
    "dispatch_order",
    ("formal_first", "correction_first"),
)
async def test_pending_correction_reuses_event_template_baseline(
    session,
    session_factory,
    tmp_path,
    dispatch_order,
):
    catalog_path = tmp_path / "event-baseline.yaml"
    catalog_path.write_text(
        """
version: generation-baseline.1
tasks:
  - code: generation.baseline
    group: emergency_technology
    phase: within_30m
    title: baseline v1
    source: test
""".strip(),
        encoding="utf-8",
    )
    suffix = uuid.uuid4().hex
    event, formal_revision = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="5.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="6",
        suffix=suffix,
        report_number=1,
        task_template_catalog_path=str(catalog_path),
    )

    catalog_path.write_text(
        """
version: generation-baseline.2
tasks:
  - code: generation.upgraded_without_request
    group: emergency_technology
    phase: within_30m
    title: baseline v2
    source: test
""".strip(),
        encoding="utf-8",
    )
    _event, correction_revision = await _ingest(
        session_factory,
        kind=EventKind.CORRECTION,
        magnitude="5.3",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="6",
        suffix=suffix,
        received_at=BASE_RECEIVED_AT + timedelta(minutes=10),
        report_number=2,
        task_template_catalog_path=str(catalog_path),
    )

    async with session_factory() as write_session:
        async with write_session.begin():
            ordinary_outboxes = (
                await write_session.scalars(
                    select(EventLifecycleOutbox)
                    .where(
                        EventLifecycleOutbox.event_id == event.id,
                        EventLifecycleOutbox.trigger_type
                        == "collaboration.requested",
                    )
                    .order_by(EventLifecycleOutbox.created_at)
                )
            ).all()
            assert len(ordinary_outboxes) == 2
            assert {
                outbox.payload["template_catalog"]["version"]
                for outbox in ordinary_outboxes
            } == {"generation-baseline.1"}

            formal_outbox = next(
                outbox
                for outbox in ordinary_outboxes
                if outbox.revision_id == formal_revision.id
            )
            correction_outbox = next(
                outbox
                for outbox in ordinary_outboxes
                if outbox.revision_id == correction_revision.id
            )
            if dispatch_order == "correction_first":
                formal_outbox.available_at = BASE_RECEIVED_AT + timedelta(
                    minutes=20
                )
                correction_outbox.available_at = BASE_RECEIVED_AT + timedelta(
                    minutes=5
                )

    dispatcher = CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=str(catalog_path),
    )
    assert await dispatcher.dispatch_once() == 2

    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    assert [task.task_code for task in tasks] == ["generation.baseline"]
    baseline_version = await session.scalar(
        select(CollaborationTaskTemplateVersion).where(
            CollaborationTaskTemplateVersion.version
            == "generation-baseline.1",
            CollaborationTaskTemplateVersion.template_code
            == "generation.baseline",
        )
    )
    assert baseline_version is not None
    assert tasks[0].template_version_id == baseline_version.id
    assert not any(
        task.task_code == "generation.upgraded_without_request"
        for task in tasks
    )


async def test_legacy_outbox_without_snapshot_uses_active_catalog(
    session,
    session_factory,
    tmp_path,
):
    catalog_path = tmp_path / "legacy-payload.yaml"
    catalog_path.write_text(
        """
version: generation-legacy.1
tasks:
  - code: generation.legacy_v1
    group: emergency_technology
    phase: within_30m
    title: legacy v1
    source: test
""".strip(),
        encoding="utf-8",
    )
    event, revision = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="5.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="6",
        suffix=uuid.uuid4().hex,
        report_number=1,
        task_template_catalog_path=str(catalog_path),
    )
    async with session_factory() as write_session:
        async with write_session.begin():
            outbox = await write_session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.event_id == event.id,
                    EventLifecycleOutbox.revision_id == revision.id,
                    EventLifecycleOutbox.trigger_type
                    == "collaboration.requested",
                )
            )
            assert outbox is not None
            outbox.payload = {
                key: value
                for key, value in outbox.payload.items()
                if key not in {"template_catalog", "template_version"}
            }

    catalog_path.write_text(
        """
version: generation-legacy.2
tasks:
  - code: generation.legacy_v2
    group: emergency_technology
    phase: within_30m
    title: legacy v2
    source: test
""".strip(),
        encoding="utf-8",
    )
    assert await CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=str(catalog_path),
    ).dispatch_once() == 1

    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    assert [task.task_code for task in tasks] == ["generation.legacy_v2"]


async def test_concurrent_first_ordinary_outboxes_share_one_template_baseline(
    session,
    session_factory,
    tmp_path,
):
    v1_path = tmp_path / "concurrent-baseline-v1.yaml"
    v1_path.write_text(
        """
version: generation-concurrent.1
tasks:
  - code: generation.concurrent_v1
    group: emergency_technology
    phase: within_30m
    title: concurrent v1
    source: test
""".strip(),
        encoding="utf-8",
    )
    v2_path = tmp_path / "concurrent-baseline-v2.yaml"
    v2_path.write_text(
        """
version: generation-concurrent.2
tasks:
  - code: generation.concurrent_v2
    group: emergency_technology
    phase: within_30m
    title: concurrent v2
    source: test
""".strip(),
        encoding="utf-8",
    )
    suffix = uuid.uuid4().hex
    event, formal_revision = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="5.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="6",
        suffix=suffix,
        report_number=1,
        task_template_catalog_path=str(v1_path),
    )
    _event, correction_revision = await _ingest(
        session_factory,
        kind=EventKind.CORRECTION,
        magnitude="5.3",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="6",
        suffix=suffix,
        received_at=BASE_RECEIVED_AT + timedelta(minutes=10),
        report_number=2,
        task_template_catalog_path=str(v2_path),
    )
    async with session_factory() as write_session:
        async with write_session.begin():
            await write_session.execute(
                delete(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.event_id == event.id,
                    EventLifecycleOutbox.trigger_type
                    == "collaboration.requested",
                )
            )

    async def enqueue(revision_id, catalog_path: Path) -> None:
        async with session_factory() as write_session:
            async with write_session.begin():
                await EventRepository(
                    session_factory,
                    task_template_catalog_path=str(catalog_path),
                ).enqueue_collaboration(
                    write_session,
                    event_id=event.id,
                    revision_id=revision_id,
                    revision_no=1,
                    trigger_reason="test",
                    created_at=BASE_RECEIVED_AT,
                )

    await asyncio.gather(
        enqueue(formal_revision.id, v1_path),
        enqueue(correction_revision.id, v2_path),
    )

    outboxes = (
        await session.scalars(
            select(EventLifecycleOutbox).where(
                EventLifecycleOutbox.event_id == event.id,
                EventLifecycleOutbox.trigger_type
                == "collaboration.requested",
            )
        )
    ).all()
    assert len(outboxes) == 2
    assert len(
        {
            outbox.payload["template_catalog"]["version"]
            for outbox in outboxes
        }
    ) == 1


async def test_correction_upgrade_adds_tasks_and_downgrade_cancels_only_pending(
    session,
    session_factory,
    tmp_path,
):
    catalog_path = tmp_path / "reconcile.yaml"
    catalog_path.write_text(
        """
version: generation-reconcile.1
tasks:
  - code: generation.base
    group: comprehensive_coordination
    phase: within_30m
    title: base
    source: test
    applicability:
      spatial_class: any
  - code: generation.high_completed
    group: comprehensive_coordination
    phase: within_30m
    title: high completed
    source: test
    applicability:
      spatial_class: any
      institutional_levels: [major, special_major]
  - code: generation.high_in_progress
    group: comprehensive_coordination
    phase: within_30m
    title: high in progress
    source: test
    applicability:
      spatial_class: any
      service_levels: ["1", "2"]
  - code: generation.high_pending
    group: comprehensive_coordination
    phase: within_30m
    title: high pending
    source: test
    applicability:
      spatial_class: any
      institutional_levels: [major, special_major]
""".strip(),
        encoding="utf-8",
    )
    suffix = uuid.uuid4().hex
    event, formal_revision = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="4.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="3",
        suffix=suffix,
        report_number=1,
        task_template_catalog_path=str(catalog_path),
    )
    dispatcher = CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=str(catalog_path),
    )
    assert await dispatcher.dispatch_once() == 1

    _event, correction_revision = await _ingest(
        session_factory,
        kind=EventKind.CORRECTION,
        magnitude="5.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="6",
        suffix=suffix,
        received_at=BASE_RECEIVED_AT + timedelta(minutes=10),
        report_number=2,
        task_template_catalog_path=str(catalog_path),
    )
    assert await dispatcher.dispatch_once() == 1

    async with session_factory() as write_session:
        async with write_session.begin():
            tasks_by_code = {
                task.task_code: task
                for task in (
                    await write_session.scalars(
                        select(WorkgroupTask).where(
                            WorkgroupTask.event_id == event.id
                        )
                    )
                ).all()
            }
            tasks_by_code["generation.high_completed"].status = "completed"
            tasks_by_code["generation.high_in_progress"].status = "in_progress"

    _event, downgrade_revision = await _ingest(
        session_factory,
        kind=EventKind.CORRECTION,
        magnitude="4.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="3",
        suffix=suffix,
        received_at=BASE_RECEIVED_AT + timedelta(minutes=20),
        report_number=3,
        task_template_catalog_path=str(catalog_path),
    )
    assert await dispatcher.dispatch_once() == 1

    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    tasks_by_code = {task.task_code: task for task in tasks}
    assert set(tasks_by_code) == {
        "generation.base",
        "generation.high_completed",
        "generation.high_in_progress",
        "generation.high_pending",
    }
    assert tasks_by_code["generation.high_completed"].status == "completed"
    assert tasks_by_code["generation.high_in_progress"].status == "in_progress"
    assert tasks_by_code["generation.high_pending"].status == "not_required"
    assert (
        tasks_by_code["generation.base"].template_version_id
        != tasks_by_code["generation.high_completed"].template_version_id
    )
    assert (
        tasks_by_code["generation.high_completed"].trigger_revision_id
        == correction_revision.id
    )
    assert formal_revision.id != correction_revision.id
    assert downgrade_revision.id != correction_revision.id

    cancellation_events = (
        await session.scalars(
            select(CollaborationTaskEvent).where(
                CollaborationTaskEvent.task_id
                == tasks_by_code["generation.high_pending"].id
            )
        )
    ).all()
    assert [
        (item.from_status, item.to_status)
        for item in cancellation_events
    ] == [
        (None, "pending"),
        ("pending", "not_required"),
    ]


async def test_correction_reconciles_frozen_template_version(
    session,
    session_factory,
    tmp_path,
):
    catalog_path = tmp_path / "versioned-reconcile.yaml"
    catalog_path.write_text(
        """
version: generation-reconcile.1
tasks:
  - code: generation.base
    group: comprehensive_coordination
    phase: within_30m
    title: base v1
    source: test
  - code: generation.legacy
    group: comprehensive_coordination
    phase: within_30m
    title: legacy
    source: test
""".strip(),
        encoding="utf-8",
    )
    suffix = uuid.uuid4().hex
    event, _formal_revision = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="4.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="3",
        suffix=suffix,
        report_number=1,
        task_template_catalog_path=str(catalog_path),
    )
    assert await CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=str(catalog_path),
    ).dispatch_once() == 1

    async with session_factory() as write_session:
        async with write_session.begin():
            v1_legacy = await write_session.scalar(
                select(WorkgroupTask).where(
                    WorkgroupTask.event_id == event.id,
                    WorkgroupTask.task_code == "generation.legacy",
                )
            )
            assert v1_legacy is not None
            v1_legacy.status = "completed"

    catalog_path.write_text(
        """
version: generation-reconcile.2
tasks:
  - code: generation.base
    group: comprehensive_coordination
    phase: within_30m
    title: base v2
    source: test
  - code: generation.new
    group: comprehensive_coordination
    phase: within_30m
    title: new
    source: test
""".strip(),
        encoding="utf-8",
    )
    _event, correction_revision = await _ingest(
        session_factory,
        kind=EventKind.CORRECTION,
        magnitude="4.3",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="4",
        suffix=suffix,
        received_at=BASE_RECEIVED_AT + timedelta(minutes=10),
        report_number=2,
        task_template_catalog_path=str(catalog_path),
    )
    correction_dispatched = await CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=str(catalog_path),
    ).dispatch_once()
    correction_outbox = await session.scalar(
        select(EventLifecycleOutbox).where(
            EventLifecycleOutbox.event_id == event.id,
            EventLifecycleOutbox.revision_id == correction_revision.id,
            EventLifecycleOutbox.trigger_type == "collaboration.requested",
        )
    )
    assert correction_dispatched == 1, (
        correction_outbox.last_error
        if correction_outbox is not None
        else "correction outbox missing"
    )

    versions = (
        await session.scalars(
            select(CollaborationTaskTemplateVersion).where(
                CollaborationTaskTemplateVersion.version.in_(
                    ("generation-reconcile.1", "generation-reconcile.2")
                )
            )
        )
    ).all()
    version_ids = {
        (version.version, version.template_code): version.id
        for version in versions
    }
    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    tasks_by_key = {
        (task.template_version_id, task.task_code): task for task in tasks
    }

    v1_base = tasks_by_key[
        (
            version_ids[("generation-reconcile.1", "generation.base")],
            "generation.base",
        )
    ]
    v1_legacy = tasks_by_key[
        (
            version_ids[("generation-reconcile.1", "generation.legacy")],
            "generation.legacy",
        )
    ]

    assert len(tasks) == 2
    assert v1_base.status == "pending"
    assert v1_legacy.status == "completed"
    assert not any(
        task.task_code == "generation.new" for task in tasks
    )
    assert {
        task.template_version_id for task in tasks
    } == {version_ids[("generation-reconcile.1", "generation.base")],
          version_ids[("generation-reconcile.1", "generation.legacy")]}
    assert correction_revision.id != _formal_revision.id


async def test_explicit_template_upgrade_switches_only_after_upgrade_request(
    session,
    session_factory,
    tmp_path,
):
    catalog_path = tmp_path / "explicit-upgrade.yaml"
    catalog_path.write_text(
        """
version: generation-upgrade.1
tasks:
  - code: generation.base
    group: comprehensive_coordination
    phase: within_30m
    title: base v1
    source: test
  - code: generation.legacy
    group: comprehensive_coordination
    phase: within_30m
    title: legacy v1
    source: test
""".strip(),
        encoding="utf-8",
    )
    suffix = uuid.uuid4().hex
    event, formal_revision = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="4.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="3",
        suffix=suffix,
        report_number=1,
        task_template_catalog_path=str(catalog_path),
    )
    assert await CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=str(catalog_path),
    ).dispatch_once() == 1

    catalog_path.write_text(
        """
version: generation-upgrade.2
tasks:
  - code: generation.base
    group: comprehensive_coordination
    phase: within_30m
    title: base v2
    source: test
  - code: generation.new
    group: comprehensive_coordination
    phase: within_30m
    title: new v2
    source: test
""".strip(),
        encoding="utf-8",
    )
    async with session_factory() as write_session:
        async with write_session.begin():
            await EventRepository(
                session_factory
            ).enqueue_collaboration_upgrade(
                write_session,
                event_id=event.id,
                revision_id=formal_revision.id,
                revision_no=formal_revision.revision_no,
                template_version="generation-upgrade.2",
                trigger_reason="recovery",
                created_at=BASE_RECEIVED_AT + timedelta(minutes=10),
            )
    assert await CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=str(catalog_path),
    ).dispatch_once() == 1

    versions = (
        await session.scalars(
            select(CollaborationTaskTemplateVersion).where(
                CollaborationTaskTemplateVersion.version.in_(
                    ("generation-upgrade.1", "generation-upgrade.2")
                )
            )
        )
    ).all()
    version_ids = {
        (version.version, version.template_code): version.id
        for version in versions
    }
    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    tasks_by_key = {
        (task.template_version_id, task.task_code): task for task in tasks
    }

    assert set(tasks_by_key) == {
        (version_ids[("generation-upgrade.1", "generation.base")], "generation.base"),
        (version_ids[("generation-upgrade.1", "generation.legacy")], "generation.legacy"),
        (version_ids[("generation-upgrade.2", "generation.base")], "generation.base"),
        (version_ids[("generation-upgrade.2", "generation.new")], "generation.new"),
    }
    assert tasks_by_key[
        (version_ids[("generation-upgrade.1", "generation.base")], "generation.base")
    ].status == "pending"
    assert tasks_by_key[
        (version_ids[("generation-upgrade.2", "generation.new")], "generation.new")
    ].status == "pending"


async def test_stale_outbox_cannot_overwrite_newer_revision_tasks(
    session,
    session_factory,
    tmp_path,
):
    catalog_path = tmp_path / "out_of_order.yaml"
    catalog_path.write_text(
        """
version: generation-out-of-order.1
tasks:
  - code: generation.base
    group: comprehensive_coordination
    phase: within_30m
    title: base
    source: test
    applicability:
      spatial_class: any
  - code: generation.major_only
    group: comprehensive_coordination
    phase: within_30m
    title: major only
    source: test
    applicability:
      spatial_class: any
      institutional_levels: [major, special_major]
""".strip(),
        encoding="utf-8",
    )
    suffix = uuid.uuid4().hex
    event, formal_revision = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="4.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="3",
        suffix=suffix,
        report_number=1,
        task_template_catalog_path=str(catalog_path),
    )
    _event, correction_revision = await _ingest(
        session_factory,
        kind=EventKind.CORRECTION,
        magnitude="5.2",
        inside_shanghai=True,
        distance_to_boundary_km="0",
        max_intensity="6",
        suffix=suffix,
        received_at=BASE_RECEIVED_AT + timedelta(minutes=10),
        report_number=2,
        task_template_catalog_path=str(catalog_path),
    )

    async with session_factory() as write_session:
        async with write_session.begin():
            stale_outbox = await write_session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == formal_revision.id,
                    EventLifecycleOutbox.trigger_type
                    == "collaboration.requested",
                )
            )
            assert stale_outbox is not None
            stale_outbox.available_at = datetime.now(UTC) + timedelta(days=1)

    dispatcher = CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path=str(catalog_path),
    )
    assert await dispatcher.dispatch_once() == 1

    async with session_factory() as write_session:
        async with write_session.begin():
            stale_outbox = await write_session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == formal_revision.id,
                    EventLifecycleOutbox.trigger_type
                    == "collaboration.requested",
                )
            )
            assert stale_outbox is not None
            stale_outbox.available_at = BASE_RECEIVED_AT

    assert await dispatcher.dispatch_once() == 1

    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    tasks_by_code = {task.task_code: task for task in tasks}
    assert set(tasks_by_code) == {"generation.base", "generation.major_only"}
    assert (
        tasks_by_code["generation.base"].trigger_revision_id
        == correction_revision.id
    )
    assert (
        tasks_by_code["generation.major_only"].trigger_revision_id
        == correction_revision.id
    )
    assert tasks_by_code["generation.major_only"].status == "pending"
