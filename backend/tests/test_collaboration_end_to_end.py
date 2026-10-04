from __future__ import annotations

import asyncio
import statistics
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select, update

from app.artifacts.catalog import load_catalog
from app.artifacts.domain import ArtifactKind
from app.assessment.repository import AssessmentRepository
from app.auth.models import User
from app.auth.service import UserRepository, ensure_superadmin
from app.collaboration.domain import DeliverableSourceKind, WorkgroupCode
from app.collaboration.models import (
    CollaborationOutbox,
    TaskDeliverable,
    TaskDeliverablePublication,
    TaskDeliverableVersion,
    WorkgroupTask,
)
from app.collaboration.service import (
    CollaborationTaskService,
    DeliverableService,
)
from app.collaboration.worker import run_worker_cycle
from app.config import settings
from app.db import SessionFactory, engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.events.response_rules import ResponseInput
from app.events.service import EventService, LifecycleIngestOutcome
from app.main import app
from app.regions.domain import RegionContext
from tests.artifact_helpers import (
    ArtifactAcceptanceEnvironment,
    ArtifactAssessmentFixture,
    SeededArtifactAssessment,
)
from tests.collaboration_expectations import EXPECTED_TASK_OUTPUT_PAIRS


@dataclass(frozen=True, slots=True)
class IngestedEvent:
    event_id: uuid.UUID
    revision_id: uuid.UUID
    received_at: datetime


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


def _all_outputs() -> tuple[tuple[str, str], ...]:
    catalog = load_catalog(settings.artifact_catalog_path)
    return tuple(
        (definition.artifact_key, definition.output_profile)
        for definition in catalog.definitions
    )


def _expected_task_output_pairs() -> set[tuple[str, str]]:
    return set(EXPECTED_TASK_OUTPUT_PAIRS)


async def _ingest(
    kind: EventKind,
    *,
    suffix: str,
    source_event_id: str | None = None,
    magnitude: str = "5.2",
    report_number: int | None = None,
    received_at: datetime | None = None,
    origin_time: datetime | None = None,
) -> IngestedEvent:
    received_at = received_at or datetime.now(UTC).replace(microsecond=0)
    source_event_id = source_event_id or f"COLLAB-E2E-{suffix}"
    event = NormalizedEvent(
        kind=kind,
        source="cenc",
        source_event_id=source_event_id,
        origin_time=origin_time or received_at - timedelta(minutes=2),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal(magnitude),
        place=f"协同端到端{kind.value}事件 {suffix}",
        report_time=received_at,
        report_number=report_number,
    )
    outcome: LifecycleIngestOutcome = await EventService(
        SessionFactory
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
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            deaths=None,
            max_intensity=Decimal("6"),
        ),
        region_context=RegionContext(
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            boundary_version="collaboration-e2e-boundary.1",
            computed_at=received_at,
        ),
    )
    return IngestedEvent(
        event_id=uuid.UUID(outcome.event_id),
        revision_id=uuid.UUID(outcome.revision_id),
        received_at=received_at,
    )


async def _event_raw_ids(event_id: uuid.UUID) -> list[uuid.UUID]:
    async with SessionFactory() as session:
        return list(
            await session.scalars(
                select(EarthquakeRevision.raw_message_id).where(
                    EarthquakeRevision.event_id == event_id
                )
            )
        )


async def _cleanup_event(event_id: uuid.UUID) -> None:
    raw_ids = await _event_raw_ids(event_id)
    async with SessionFactory() as session:
        async with session.begin():
            task_ids = select(WorkgroupTask.id).where(
                WorkgroupTask.event_id == event_id
            )
            deliverable_ids = select(TaskDeliverable.id).where(
                TaskDeliverable.task_id.in_(task_ids)
            )
            await session.execute(
                delete(TaskDeliverablePublication).where(
                    TaskDeliverablePublication.deliverable_id.in_(
                        deliverable_ids
                    )
                )
            )
            await session.execute(
                update(TaskDeliverableVersion)
                .where(
                    TaskDeliverableVersion.deliverable_id.in_(
                        deliverable_ids
                    )
                )
                .values(supersedes_version_id=None)
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
                delete(EarthquakeEvent).where(EarthquakeEvent.id == event_id)
            )
            if raw_ids:
                await session.execute(
                    delete(RawMessage).where(RawMessage.id.in_(raw_ids))
                )


async def _superadmin_actor() -> User:
    async with SessionFactory() as session:
        async with session.begin():
            actor = await ensure_superadmin(
                session,
                UserRepository(SessionFactory),
            )
            await session.refresh(actor)
            return actor


async def _command_hall_client() -> httpx.AsyncClient:
    await _superadmin_actor()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://command-hall-e2e",
    )
    response = await client.post(
        "/api/v1/auth/login",
        data={
            "username": settings.superadmin_username,
            "password": (
                settings.superadmin_initial_password.get_secret_value()
            ),
        },
    )
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = (
        f"Bearer {response.json()['access_token']}"
    )
    return client


async def _overview_payload(
    client: httpx.AsyncClient,
    event_id: uuid.UUID,
) -> dict[str, Any] | None:
    response = await client.get(
        f"/api/v1/command-hall/events/{event_id}/overview"
    )
    if response.status_code == 404:
        return None
    assert response.status_code == 200, response.text
    return response.json()


async def _wait_for_visible_tasks(
    client: httpx.AsyncClient,
    event_id: uuid.UUID,
    *,
    timeout_seconds: float = 8.0,
) -> float:
    started = time.perf_counter()
    deadline = started + timeout_seconds
    while time.perf_counter() < deadline:
        await run_worker_cycle(
            SessionFactory,
            datetime.now(UTC),
            configured=settings,
        )
        overview = await _overview_payload(client, event_id)
        if (
            overview is not None
            and overview["task_counts"]["total"] == 60
            and overview["group_count"] == 7
        ):
            return time.perf_counter() - started
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"Command Hall tasks were not visible within {timeout_seconds}s"
    )


async def _assert_real_worker_chain(event_id: uuid.UUID) -> None:
    async with SessionFactory() as session:
        lifecycle = await session.scalar(
            select(EventLifecycleOutbox).where(
                EventLifecycleOutbox.event_id == event_id,
                EventLifecycleOutbox.trigger_type
                == "collaboration.requested",
            )
        )
        projection = await session.scalar(
            select(CollaborationOutbox).where(
                CollaborationOutbox.event_id == event_id,
                CollaborationOutbox.event_type == "task.generated",
            )
        )
    assert lifecycle is not None
    assert lifecycle.status == "published"
    assert lifecycle.attempt_count >= 1
    assert lifecycle.published_at is not None
    assert projection is not None
    assert projection.status == "published"
    assert projection.attempt_count >= 1
    assert projection.dispatched_at is not None


async def _linked_pairs(
    event_id: uuid.UUID,
) -> set[tuple[str, str]]:
    async with SessionFactory() as session:
        rows = await session.execute(
            select(
                WorkgroupTask.task_code,
                TaskDeliverable.deliverable_code,
            )
            .join(
                TaskDeliverable,
                TaskDeliverable.task_id == WorkgroupTask.id,
            )
            .join(
                TaskDeliverableVersion,
                TaskDeliverableVersion.deliverable_id
                == TaskDeliverable.id,
            )
            .where(
                WorkgroupTask.event_id == event_id,
                TaskDeliverableVersion.source_kind
                == DeliverableSourceKind.AUTOMATIC.value,
            )
        )
        return {(str(task_code), str(output_code)) for task_code, output_code in rows}


async def _wait_for_linked_pairs(
    event_id: uuid.UUID,
    expected: set[tuple[str, str]],
    *,
    timeout_seconds: float = 20.0,
) -> None:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        await run_worker_cycle(
            SessionFactory,
            datetime.now(UTC),
            configured=settings,
        )
        if expected <= await _linked_pairs(event_id):
            return
        await asyncio.sleep(0.05)
    actual = await _linked_pairs(event_id)
    raise AssertionError(
        f"linked task/output pairs did not converge; missing={expected - actual}"
    )


async def _assessment_run(event_id: uuid.UUID) -> uuid.UUID:
    async with SessionFactory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.event_id == event_id,
                    EventLifecycleOutbox.trigger_type
                    == "assessment.requested",
                )
            )
            if outbox is None:
                raise AssertionError("assessment outbox was not created")
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=event_id,
                revision_id=outbox.revision_id,
                outbox_id=outbox.id,
            )
            return run.id


async def _artifact_environment(
    ingested: IngestedEvent,
) -> tuple[ArtifactAcceptanceEnvironment, Any]:
    assessment_run_id = await _assessment_run(ingested.event_id)
    catalog = load_catalog(settings.artifact_catalog_path)
    deadline_at = ingested.received_at + timedelta(seconds=300)
    seeded = SeededArtifactAssessment(
        event_id=ingested.event_id,
        revision_id=ingested.revision_id,
        assessment_run_id=assessment_run_id,
        deadline_basis_at=ingested.received_at,
        deadline_at=deadline_at,
        catalog=catalog,
    )
    fixture = ArtifactAssessmentFixture(SessionFactory, seeded)
    return ArtifactAcceptanceEnvironment(SessionFactory, fixture), seeded


async def _run_artifacts(
    ingested: IngestedEvent,
    *,
    production_mode: str,
    required_outputs: tuple[tuple[str, str], ...] | None = None,
):
    environment, _seeded = await _artifact_environment(ingested)
    try:
        return await environment.run_selected_outputs(
            required_outputs=required_outputs or _all_outputs(),
            production_mode=production_mode,
            magnitude=5.2,
        )
    finally:
        await environment.cleanup()


async def _ensure_dual_versions(
    event_id: uuid.UUID,
    actor: User,
) -> None:
    service = DeliverableService()
    async with SessionFactory() as session:
        row = (
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
                    WorkgroupTask.task_code == "technology.rapid_brief",
                    TaskDeliverable.deliverable_code
                    == "doc.rapid_brief",
                    TaskDeliverableVersion.source_kind
                    == DeliverableSourceKind.AUTOMATIC.value,
                )
                .order_by(TaskDeliverableVersion.version_no)
                .limit(1)
            )
        ).first()
        if row is None:
            raise AssertionError("automatic rapid brief was not linked")
        deliverable, automatic = row

    async with SessionFactory() as session:
        async with session.begin():
            current = await service.repository.get_current_publication(
                session,
                deliverable.id,
            )
            if current is None:
                await service.publish(
                    session,
                    deliverable.id,
                    actor,
                    version_id=automatic.id,
                    publication_note="E2E automatic publication",
                )

    async with SessionFactory() as session:
        async with session.begin():
            manual_id = await session.scalar(
                select(TaskDeliverableVersion.id)
                .where(
                    TaskDeliverableVersion.deliverable_id
                    == deliverable.id,
                    TaskDeliverableVersion.source_kind
                    == DeliverableSourceKind.MANUAL.value,
                )
                .order_by(TaskDeliverableVersion.version_no.desc())
                .limit(1)
            )
            if manual_id is None:
                manual = await service.add_text_version(
                    session,
                    deliverable.id,
                    actor,
                    text_result={"result": "人工校核后的快速评估简报"},
                    basis_text="E2E manual revision",
                )
                manual_id = manual.id

    async with SessionFactory() as session:
        async with session.begin():
            current = await service.repository.get_current_publication(
                session,
                deliverable.id,
            )
            if current is None or current.version_id != manual_id:
                await service.publish(
                    session,
                    deliverable.id,
                    actor,
                    version_id=manual_id,
                    publication_note="E2E manual publication",
                )


async def _publish_latest_versions(
    event_id: uuid.UUID,
    actor: User,
) -> None:
    service = DeliverableService()
    async with SessionFactory() as session:
        deliverable_ids = list(
            await session.scalars(
                select(TaskDeliverable.id)
                .join(
                    WorkgroupTask,
                    WorkgroupTask.id == TaskDeliverable.task_id,
                )
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


async def _close_all_tasks(
    event_id: uuid.UUID,
    actor: User,
) -> int:
    await _publish_latest_versions(event_id, actor)
    service = CollaborationTaskService()
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


async def _refresh_projection(
    event_id: uuid.UUID,
    *,
    timeout_seconds: float = 180.0,
) -> None:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        await run_worker_cycle(
            SessionFactory,
            datetime.now(UTC),
            configured=settings,
        )
        async with SessionFactory() as session:
            pending = list(
                await session.scalars(
                    select(CollaborationOutbox).where(
                        CollaborationOutbox.event_id == event_id,
                        CollaborationOutbox.status.in_(
                            ("pending", "processing", "dead_letter")
                        ),
                    )
                )
            )
        if not pending:
            return
        if any(item.status == "dead_letter" for item in pending):
            raise AssertionError(
                "collaboration projection outbox reached dead-letter: "
                f"{pending[0].last_error}"
            )
        await asyncio.sleep(0.05)
    raise AssertionError(
        "collaboration projection outbox did not drain: "
        f"pending={len(pending)} "
        f"statuses={sorted({item.status for item in pending})} "
        f"last_error={pending[0].last_error if pending else None}"
    )


async def test_formal_event_closes_all_seven_groups_with_dual_versions() -> None:
    formal = await _ingest(
        EventKind.FORMAL,
        suffix=uuid.uuid4().hex,
        report_number=1,
    )
    client = await _command_hall_client()
    try:
        visibility_latency = await _wait_for_visible_tasks(
            client,
            formal.event_id,
        )
        assert visibility_latency <= 5.0
        await _assert_real_worker_chain(formal.event_id)

        async with SessionFactory() as session:
            tasks = list(
                await session.scalars(
                    select(WorkgroupTask).where(
                        WorkgroupTask.event_id == formal.event_id
                    )
                )
            )
        assert {task.workgroup_code for task in tasks} == {
            group.value for group in WorkgroupCode
        }
        assert len(tasks) == 60

        artifact_result = await _run_artifacts(
            formal,
            production_mode="live",
        )
        assert artifact_result.complete_count == 28
        assert artifact_result.degraded_count == 11
        assert artifact_result.failed_count == 0
        assert artifact_result.timeout_count == 0
        assert artifact_result.required_output_count == 39

        expected_pairs = _expected_task_output_pairs()
        await _wait_for_linked_pairs(formal.event_id, expected_pairs)
        linked_pairs = await _linked_pairs(formal.event_id)
        assert expected_pairs <= linked_pairs
        artifact_keys = {
            definition.artifact_key
            for definition in load_catalog(
                settings.artifact_catalog_path
            ).definitions
        }
        assert {artifact_key for _, artifact_key in expected_pairs} == artifact_keys
        map_keys = {
            definition.artifact_key
            for definition in load_catalog(
                settings.artifact_catalog_path
            ).definitions
            if definition.kind is ArtifactKind.MAP
        }
        assert len(map_keys) == 27
        assert map_keys <= {
            artifact_key for _, artifact_key in linked_pairs
        }

        actor = await _superadmin_actor()
        await _ensure_dual_versions(formal.event_id, actor)
        completed = await _close_all_tasks(formal.event_id, actor)
        assert completed == 60
        await _refresh_projection(formal.event_id)

        overview = await _overview_payload(client, formal.event_id)
        assert overview is not None
        assert overview["task_counts"]["completed"] == 60
        assert overview["task_counts"]["overdue"] == 0
        assert overview["task_counts"]["failed"] == 0
        assert overview["dual_version_count"] >= 1

        async with SessionFactory() as session:
            rapid_brief_deliverable = await session.scalar(
                select(TaskDeliverable)
                .join(
                    WorkgroupTask,
                    WorkgroupTask.id == TaskDeliverable.task_id,
                )
                .where(
                    WorkgroupTask.event_id == formal.event_id,
                    WorkgroupTask.task_code == "technology.rapid_brief",
                    TaskDeliverable.deliverable_code == "doc.rapid_brief",
                )
            )
            versions = list(
                await session.scalars(
                    select(TaskDeliverableVersion)
                    .where(
                        TaskDeliverableVersion.deliverable_id
                        == rapid_brief_deliverable.id
                    )
                    .order_by(TaskDeliverableVersion.version_no)
                )
            )
            current = await session.scalar(
                select(TaskDeliverablePublication).where(
                    TaskDeliverablePublication.deliverable_id
                    == rapid_brief_deliverable.id,
                    TaskDeliverablePublication.superseded_at.is_(None),
                )
            )
        assert {version.source_kind for version in versions} == {
            DeliverableSourceKind.AUTOMATIC.value,
            DeliverableSourceKind.MANUAL.value,
        }
        assert current is not None
        assert current.version_id in {
            version.id
            for version in versions
            if version.source_kind == DeliverableSourceKind.MANUAL.value
        }
    finally:
        await client.aclose()
        await _cleanup_event(formal.event_id)


async def test_correction_keeps_frozen_version_and_drill_flows_through_worker() -> None:
    source_event_id = f"COLLAB-CORRECTION-{uuid.uuid4().hex}"
    formal = await _ingest(
        EventKind.FORMAL,
        suffix=uuid.uuid4().hex,
        source_event_id=source_event_id,
        report_number=1,
    )
    drill_received_at = datetime.now(UTC).replace(microsecond=0)
    drill = await _ingest(
        EventKind.DRILL,
        suffix=uuid.uuid4().hex,
        received_at=drill_received_at,
        origin_time=drill_received_at - timedelta(minutes=30),
    )
    async with SessionFactory() as session:
        drill_event = await session.get(EarthquakeEvent, drill.event_id)
        drill_revision = await session.get(
            EarthquakeRevision,
            drill.revision_id,
        )
    assert drill_event is not None
    assert drill_revision is not None
    assert drill_event.event_type == EventKind.DRILL.value
    assert drill_event.current_revision_id == drill.revision_id
    assert drill_revision.is_current is True
    client = await _command_hall_client()
    try:
        await _wait_for_visible_tasks(client, formal.event_id)
        async with SessionFactory() as session:
            formal_tasks = list(
                await session.scalars(
                    select(WorkgroupTask).where(
                        WorkgroupTask.event_id == formal.event_id
                    )
                )
            )
        formal_task_ids = {task.id for task in formal_tasks}
        formal_version_ids = {
            task.template_version_id for task in formal_tasks
        }

        correction = await _ingest(
            EventKind.CORRECTION,
            suffix=uuid.uuid4().hex,
            source_event_id=source_event_id,
            magnitude="5.4",
            report_number=2,
        )
        assert correction.event_id == formal.event_id
        await _wait_for_visible_tasks(client, formal.event_id)
        async with SessionFactory() as session:
            corrected_tasks = list(
                await session.scalars(
                    select(WorkgroupTask).where(
                        WorkgroupTask.event_id == formal.event_id
                    )
                )
            )
        assert {task.id for task in corrected_tasks} == formal_task_ids
        assert {
            task.template_version_id for task in corrected_tasks
        } == formal_version_ids
        assert all(
            task.template_version_id is not None
            for task in corrected_tasks
        )

        await _wait_for_visible_tasks(client, drill.event_id)
        artifact_result = await _run_artifacts(
            drill,
            production_mode="drill",
            required_outputs=(("doc.background", "a3v-professional"),),
        )
        assert artifact_result.failed_count == 0
        assert artifact_result.timeout_count == 0
        assert any(
            "【演练】" in artifact.file_name
            for artifact in artifact_result.artifacts
        )
        await _wait_for_linked_pairs(
            drill.event_id,
            {("damage.background_materials", "doc.background")},
        )
        await _assert_real_worker_chain(drill.event_id)

        async with SessionFactory() as session:
            event = await session.get(EarthquakeEvent, drill.event_id)
            task_count = len(
                list(
                    await session.scalars(
                        select(WorkgroupTask.id).where(
                            WorkgroupTask.event_id == drill.event_id
                        )
                    )
                )
            )
        assert event is not None
        assert event.event_type == EventKind.DRILL.value
        assert task_count == 60
    finally:
        await client.aclose()
        await _cleanup_event(formal.event_id)
        await _cleanup_event(drill.event_id)


@pytest.mark.performance
async def test_formal_and_manual_ingress_to_command_hall_p95_targets() -> None:
    count = 20
    events: list[IngestedEvent] = []
    generation_samples: list[float] = []
    overview_samples: list[float] = []
    client = await _command_hall_client()
    try:
        for index in range(count):
            kind = EventKind.FORMAL if index % 2 == 0 else EventKind.MANUAL
            started = time.perf_counter()
            ingested = await _ingest(
                kind,
                suffix=f"P95-{index}-{uuid.uuid4().hex}",
                report_number=index + 1 if kind is EventKind.FORMAL else None,
            )
            await _wait_for_visible_tasks(client, ingested.event_id)
            generation_samples.append(time.perf_counter() - started)
            events.append(ingested)

            for _ in range(3):
                overview_started = time.perf_counter()
                overview = await _overview_payload(client, ingested.event_id)
                overview_samples.append(
                    time.perf_counter() - overview_started
                )
                assert overview is not None
                assert overview["task_counts"]["total"] == 60
                assert overview["group_count"] == 7
            await _assert_real_worker_chain(ingested.event_id)

        generation_p95 = _p95(generation_samples)
        overview_p95 = _p95(overview_samples)
        assert generation_p95 <= 5.0
        assert overview_p95 <= 0.5
        assert statistics.median(generation_samples) <= 5.0
        assert statistics.median(overview_samples) <= 0.5
    finally:
        await client.aclose()
        for event in events:
            await _cleanup_event(event.event_id)
