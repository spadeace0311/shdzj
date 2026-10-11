from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from PIL import Image, ImageDraw
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, func, select

from app.artifacts.models import (
    ArtifactOverrideRequest,
    ArtifactPublication,
    GeneratedArtifact,
    ProductionRun,
    ProductionTask,
)
from app.artifacts.service import (
    ArtifactOverrideResponse,
    ArtifactOverrideService,
    ArtifactOverrideValidationError,
    IdempotencyConflictError,
    sha256_json,
)
from app.artifacts.storage import ArtifactStore
from app.collaboration.models import CollaborationSettings
from app.db import engine
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.operations.models import OperationsAuditLog, OperationsRun
from app.operations.priority import (
    OperationsPriorityGuard,
    OperationsWorkBlocked,
    RealEventPriorityActive,
)
from app.operations.repository import OperationsRepository
from app.operations.schemas import DrillTriggerRequest
from app.operations.service import OperationsService
from app.regions.models import RegionBoundary


@pytest.fixture(autouse=True)
async def dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


def _jpeg_bytes(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (4761, 3369), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 1200, 1200), fill=color)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=35, dpi=(300, 300))
    return output.getvalue()


def _marked_test_jpeg_bytes() -> bytes:
    image = Image.new("RGB", (4761, 3369), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 1200, 1200), fill=(0, 0, 255))
    image.info["comment"] = "【测试】"
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=35, dpi=(300, 300))
    return output.getvalue()


def _marked_drill_jpeg_bytes() -> bytes:
    image = Image.new("RGB", (4761, 3369), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 1200, 1200), fill=(0, 0, 255))
    image.info["comment"] = "【演练】"
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=35, dpi=(300, 300))
    return output.getvalue()


_VALID_JPEG_BYTES = _jpeg_bytes((255, 0, 0))
_MARKED_TEST_JPEG_BYTES = _marked_test_jpeg_bytes()
_MARKED_DRILL_JPEG_BYTES = _marked_drill_jpeg_bytes()
_OTHER_JPEG_BYTES = _jpeg_bytes((0, 0, 255))
_VALID_JPEG_CHECKSUM = hashlib.sha256(_VALID_JPEG_BYTES).hexdigest()
_VALID_JPEG_SIZE = len(_VALID_JPEG_BYTES)
_LOCK_ORDER_OBSERVED_AT = datetime(2040, 1, 15, 4, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class EventReference:
    id: uuid.UUID
    current_revision_id: uuid.UUID


class _OverrideLockOrderGuard:
    def __init__(
        self,
        *,
        event_locked: asyncio.Event,
        resume_reached: asyncio.Event,
    ) -> None:
        self.event_locked = event_locked
        self.resume_reached = resume_reached
        self._guard = OperationsPriorityGuard()

    async def ensure_event_work_allowed(self, session, event_id) -> None:
        self.event_locked.set()
        await self.resume_reached.wait()
        await self._guard.ensure_event_work_allowed(session, event_id)

    async def has_active_real_event(self, session, observed_at) -> bool:
        return await self._guard.has_active_real_event(
            session,
            observed_at,
        )


class _ResumeReplayLockOrderService(OperationsService):
    def __init__(self, *args, resume_reached: asyncio.Event, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.resume_reached = resume_reached

    async def _validate_resume_replacement(
        self,
        session,
        source,
        replacement,
        source_run_id,
    ) -> None:
        self.resume_reached.set()
        await super()._validate_resume_replacement(
            session,
            source,
            replacement,
            source_run_id,
        )


@pytest.fixture
def valid_override_jpeg() -> io.BytesIO:
    return io.BytesIO(_VALID_JPEG_BYTES)


@pytest.fixture
def other_override_jpeg() -> io.BytesIO:
    return io.BytesIO(_OTHER_JPEG_BYTES)


@pytest.fixture
def seeded_event(seeded_artifact_assessment) -> EventReference:
    return EventReference(
        id=seeded_artifact_assessment.event_id,
        current_revision_id=seeded_artifact_assessment.revision_id,
    )


async def _ensure_operations_lock_order_environment(session) -> None:
    active_boundary = await session.scalar(
        select(RegionBoundary)
        .where(RegionBoundary.is_active.is_(True))
        .limit(1)
    )
    if active_boundary is None:
        session.add(
            RegionBoundary(
                version=f"override-lock-boundary-{uuid.uuid4().hex}",
                name="override lock boundary",
                local_buffer_km=Decimal("50"),
                geom=WKTElement(
                    "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, "
                    "122.2 31.8, 121.0 31.8, 121.0 30.8)))",
                    srid=4326,
                ),
                maritime_geom=WKTElement(
                    "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, "
                    "122.2 31.8, 121.0 31.8, 121.0 30.8)))",
                    srid=4326,
                ),
                source_uri="test://override-lock-boundary",
                checksum="a" * 64,
                is_active=True,
            )
        )
    collaboration_settings = await session.scalar(
        select(CollaborationSettings.id).limit(1)
    )
    if collaboration_settings is None:
        session.add(
            CollaborationSettings(
                id=uuid.UUID("00000000-0000-0000-0000-000000000024"),
                intensity_threshold=Decimal("2.0"),
                row_version=1,
            )
        )
    await OperationsRepository().get_settings(session)


async def _cleanup_lock_order_event(
    session_factory,
    event_id: uuid.UUID,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(ArtifactOverrideRequest).where(
                    ArtifactOverrideRequest.event_id == event_id
                )
            )
            production_ids = list(
                await session.scalars(
                    select(ProductionRun.id).where(
                        ProductionRun.event_id == event_id
                    )
                )
            )
            await session.execute(
                delete(ArtifactPublication).where(
                    ArtifactPublication.production_run_id.in_(
                        production_ids
                    )
                )
            )
            await session.execute(
                delete(GeneratedArtifact).where(
                    GeneratedArtifact.production_run_id.in_(
                        production_ids
                    )
                )
            )
            await session.execute(
                delete(ProductionTask).where(
                    ProductionTask.production_run_id.in_(
                        production_ids
                    )
                )
            )
            await session.execute(
                delete(ProductionRun).where(
                    ProductionRun.event_id == event_id
                )
            )
            run_ids = list(
                await session.scalars(
                    select(OperationsRun.id).where(
                        OperationsRun.event_id == event_id
                    )
                )
            )
            await session.execute(
                delete(OperationsAuditLog).where(
                    OperationsAuditLog.resource_id.in_(
                        [str(run_id) for run_id in run_ids]
                    )
                )
            )
            await session.execute(
                delete(OperationsRun).where(
                    OperationsRun.event_id == event_id
                )
            )
            await session.execute(
                delete(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.event_id == event_id
                )
            )
            raw_ids = list(
                await session.scalars(
                    select(EarthquakeRevision.raw_message_id).where(
                        EarthquakeRevision.event_id == event_id
                    )
                )
            )
            await session.execute(
                delete(EarthquakeEvent).where(
                    EarthquakeEvent.id == event_id
                )
            )
            await session.execute(
                delete(RawMessage).where(RawMessage.id.in_(raw_ids))
            )


@pytest.fixture
def artifact_override_service(session_factory, tmp_path) -> ArtifactOverrideService:
    return ArtifactOverrideService(
        session_factory=session_factory,
        storage_root=tmp_path / "artifacts",
        lease_seconds=2.0,
        poll_interval=0.01,
    )


async def test_same_idempotency_key_and_fingerprint_returns_same_result(
    artifact_override_service,
    seeded_event,
    valid_override_jpeg,
) -> None:
    first = await artifact_override_service.override(
        actor_id="admin-id",
        event_id=seeded_event.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        revision_id=seeded_event.current_revision_id,
        reason="替换错误图件",
        expected_current_artifact_id=None,
        idempotency_key="00000000-0000-0000-0000-000000000001",
        upload=valid_override_jpeg,
    )
    second = await artifact_override_service.override(
        actor_id="admin-id",
        event_id=seeded_event.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        revision_id=seeded_event.current_revision_id,
        reason="替换错误图件",
        expected_current_artifact_id=None,
        idempotency_key="00000000-0000-0000-0000-000000000001",
        upload=valid_override_jpeg,
    )

    assert second.artifact_id == first.artifact_id
    assert second.production_run_id == first.production_run_id


async def test_resume_replay_and_override_do_not_deadlock_on_same_event(
    session_factory,
    tmp_path,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await _ensure_operations_lock_order_environment(session)

    operations_service = OperationsService(
        session_factory=session_factory,
        now=lambda: _LOCK_ORDER_OBSERVED_AT,
    )
    source = await operations_service.trigger_drill(
        DrillTriggerRequest(
            idempotency_key=f"override-lock-source-{uuid.uuid4()}"
        ),
        "operator",
    )
    assert source.run_id is not None and source.event_id is not None

    async with session_factory() as session:
        async with session.begin():
            source_run = await session.get(OperationsRun, source.run_id)
            assert source_run is not None
            source_run.operation_state = "paused"
            source_run.resume_required = True
            source_run.paused_at = _LOCK_ORDER_OBSERVED_AT

    replacement = await operations_service.resume_drill(
        source.run_id,
        "superadmin",
    )
    assert (
        replacement.run_id is not None
        and replacement.event_id is not None
        and replacement.revision_id is not None
    )

    event_locked = asyncio.Event()
    resume_reached = asyncio.Event()
    override_guard = _OverrideLockOrderGuard(
        event_locked=event_locked,
        resume_reached=resume_reached,
    )
    override_service = ArtifactOverrideService(
        session_factory=session_factory,
        storage_root=tmp_path / "artifacts",
        lease_seconds=2.0,
        poll_interval=0.01,
        priority_guard=override_guard,
    )
    replay_service = _ResumeReplayLockOrderService(
        session_factory,
        now=lambda: _LOCK_ORDER_OBSERVED_AT,
        resume_reached=resume_reached,
    )

    try:
        override_task = asyncio.create_task(
            override_service.override(
                actor_id="superadmin",
                event_id=replacement.event_id,
                artifact_key="map.epicenter",
                output_profile="a3v-professional",
                revision_id=uuid.UUID(replacement.revision_id),
                reason="并发覆盖",
                expected_current_artifact_id=None,
                idempotency_key=str(uuid.uuid4()),
                upload=io.BytesIO(_MARKED_DRILL_JPEG_BYTES),
            )
        )
        await asyncio.wait_for(event_locked.wait(), timeout=10)

        replay_task = asyncio.create_task(
            replay_service.resume_drill(source.run_id, "superadmin")
        )
        await asyncio.wait_for(resume_reached.wait(), timeout=10)
        override_result, replay_result = await asyncio.gather(
            override_task,
            replay_task,
            return_exceptions=True,
        )

        assert not isinstance(override_result, Exception)
        assert not isinstance(replay_result, Exception)
        assert override_result.production_mode == "drill"
        assert override_result.marker == "drill"
        assert replay_result.run_id == replacement.run_id
        assert replay_result.event_id == replacement.event_id

        async with session_factory() as session:
            source_run = await session.get(OperationsRun, source.run_id)

        assert source_run is not None
        assert source_run.operation_state == "canceled"
        assert source_run.resume_required is False
    finally:
        await _cleanup_lock_order_event(
            session_factory,
            replacement.event_id,
        )
        await _cleanup_lock_order_event(
            session_factory,
            source.event_id,
        )


async def test_same_idempotency_key_with_different_fingerprint_conflicts(
    artifact_override_service,
    seeded_event,
    valid_override_jpeg,
    other_override_jpeg,
) -> None:
    await artifact_override_service.override(
        actor_id="admin-id",
        event_id=seeded_event.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        revision_id=seeded_event.current_revision_id,
        reason="第一次原因",
        expected_current_artifact_id=None,
        idempotency_key="00000000-0000-0000-0000-000000000002",
        upload=valid_override_jpeg,
    )

    with pytest.raises(IdempotencyConflictError):
        await artifact_override_service.override(
            actor_id="admin-id",
            event_id=seeded_event.id,
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            revision_id=seeded_event.current_revision_id,
            reason="第二次原因",
            expected_current_artifact_id=None,
            idempotency_key="00000000-0000-0000-0000-000000000002",
            upload=other_override_jpeg,
        )


async def test_concurrent_same_key_creates_one_run(
    artifact_override_service,
    seeded_event,
    session_factory,
) -> None:
    async def invoke():
        return await artifact_override_service.override(
            actor_id="admin-id",
            event_id=seeded_event.id,
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            revision_id=seeded_event.current_revision_id,
            reason="并发覆盖",
            expected_current_artifact_id=None,
            idempotency_key="00000000-0000-0000-0000-000000000003",
            upload=io.BytesIO(_VALID_JPEG_BYTES),
        )

    first, second = await asyncio.gather(invoke(), invoke())

    assert first.artifact_id == second.artifact_id
    assert first.production_run_id == second.production_run_id

    async with session_factory() as session:
        run_count = await session.scalar(
            select(func.count())
            .select_from(ProductionRun)
            .where(ProductionRun.id == first.production_run_id)
        )
        artifact_count = await session.scalar(
            select(func.count())
            .select_from(GeneratedArtifact)
            .where(GeneratedArtifact.production_run_id == first.production_run_id)
        )
        publication_count = await session.scalar(
            select(func.count())
            .select_from(ArtifactPublication)
            .where(ArtifactPublication.production_run_id == first.production_run_id)
        )

    assert run_count == 1
    assert artifact_count == 1
    assert publication_count == 1


async def test_expected_current_artifact_id_mismatch_does_not_publish(
    artifact_override_service,
    seeded_artifact_assessment,
    seeded_event,
    session_factory,
) -> None:
    published = await seeded_artifact_assessment.published_artifact(
        "map.epicenter",
        version=1,
    )

    with pytest.raises(IdempotencyConflictError):
        await artifact_override_service.override(
            actor_id="admin-id",
            event_id=seeded_event.id,
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            revision_id=seeded_event.current_revision_id,
            reason="错误版本预期",
            expected_current_artifact_id=uuid.uuid4(),
            idempotency_key="00000000-0000-0000-0000-000000000006",
            upload=io.BytesIO(_VALID_JPEG_BYTES),
        )

    async with session_factory() as session:
        current = await session.scalar(
            select(ArtifactPublication).where(
                ArtifactPublication.event_id == published.event_id,
                ArtifactPublication.artifact_key == published.artifact_key,
                ArtifactPublication.output_profile == published.output_profile,
                ArtifactPublication.production_mode == published.production_mode,
                ArtifactPublication.superseded_at.is_(None),
            )
        )

    assert current is not None
    assert current.artifact_id == published.id


async def test_test_override_forces_filename_marker_and_metadata(
    artifact_override_service,
    seeded_event,
    session_factory,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            event = await session.get(EarthquakeEvent, seeded_event.id)
            revision = await session.get(
                EarthquakeRevision,
                seeded_event.current_revision_id,
            )
            assert event is not None and revision is not None
            event.event_type = "test"
            revision.revision_kind = "test"

    response = await artifact_override_service.override(
        actor_id="admin-id",
        event_id=seeded_event.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        revision_id=seeded_event.current_revision_id,
        reason="标记化覆盖",
        expected_current_artifact_id=None,
        idempotency_key="00000000-0000-0000-0000-000000000101",
        upload=io.BytesIO(_MARKED_TEST_JPEG_BYTES),
    )

    assert response.production_mode == "test"
    assert response.marker == "test"
    assert response.file_name.startswith("【测试】")
    async with session_factory() as session:
        artifact = await session.get(
            GeneratedArtifact,
            response.artifact_id,
        )
    assert artifact is not None
    assert artifact.marker == {
        "marker": "【测试】",
        "operation_marker": "test",
    }


async def test_override_rejects_paused_test_operation(
    artifact_override_service,
    seeded_event,
    session_factory,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            event = await session.get(EarthquakeEvent, seeded_event.id)
            revision = await session.get(
                EarthquakeRevision,
                seeded_event.current_revision_id,
            )
            assert event is not None and revision is not None
            event.event_type = "test"
            revision.revision_kind = "test"
            session.add(
                OperationsRun(
                    event_id=event.id,
                    trigger_mode="manual_test",
                    operation_state="paused",
                    priority=100,
                    idempotency_key="paused-override-test",
                    triggered_by="test",
                    resume_required=True,
                    detail={},
                )
            )

    with pytest.raises(OperationsWorkBlocked):
        await artifact_override_service.override(
            actor_id="admin-id",
            event_id=seeded_event.id,
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            revision_id=seeded_event.current_revision_id,
            reason="暂停覆盖",
            expected_current_artifact_id=None,
            idempotency_key="00000000-0000-0000-0000-000000000102",
            upload=io.BytesIO(_MARKED_TEST_JPEG_BYTES),
        )


async def test_override_rejects_active_real_event(
    artifact_override_service,
    seeded_event,
    session_factory,
) -> None:
    real_id = uuid.uuid4()
    async with session_factory() as session:
        async with session.begin():
            target_event = await session.get(
                EarthquakeEvent,
                seeded_event.id,
            )
            target_revision = await session.get(
                EarthquakeRevision,
                seeded_event.current_revision_id,
            )
            assert target_event is not None and target_revision is not None
            target_event.event_type = "test"
            target_revision.revision_kind = "test"
            event = EarthquakeEvent(
                id=real_id,
                source="artifact-real-priority",
                canonical_source_id=f"artifact-real-priority:{real_id}",
                event_type="manual",
                origin_time=datetime.now(UTC),
                longitude=Decimal("121.500000"),
                latitude=Decimal("31.200000"),
                depth_km=Decimal("10.00"),
                magnitude=Decimal("5.2"),
                place="real priority",
                geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                lifecycle_state="not_applicable",
                real_event_activated_at=datetime.now(UTC),
            )
            session.add(event)
            await session.flush()
            raw = RawMessage(
                id=uuid.uuid4(),
                source="artifact-real-priority",
                source_message_id=uuid.uuid4().hex,
                message_kind="manual",
                checksum=uuid.uuid4().hex,
                payload={"event_id": str(real_id)},
            )
            session.add(raw)
            await session.flush()
            revision = EarthquakeRevision(
                id=uuid.uuid4(),
                event_id=real_id,
                raw_message_id=raw.id,
                revision_no=1,
                revision_kind="manual",
                origin_time=event.origin_time,
                longitude=event.longitude,
                latitude=event.latitude,
                depth_km=event.depth_km,
                magnitude=event.magnitude,
                place=event.place,
                is_current=True,
                ingested_at=datetime.now(UTC),
            )
            session.add(revision)
            await session.flush()
            event.current_revision_id = revision.id

    try:
        with pytest.raises(RealEventPriorityActive):
            await artifact_override_service.override(
                actor_id="admin-id",
                event_id=seeded_event.id,
                artifact_key="map.epicenter",
                output_profile="a3v-professional",
                revision_id=seeded_event.current_revision_id,
                reason="真实事件覆盖",
                expected_current_artifact_id=None,
                idempotency_key="00000000-0000-0000-0000-000000000103",
                upload=io.BytesIO(_MARKED_TEST_JPEG_BYTES),
            )
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(EarthquakeEvent).where(
                        EarthquakeEvent.id == real_id
                    )
                )
                await session.execute(
                    delete(RawMessage).where(
                        RawMessage.source == "artifact-real-priority"
                    )
                )


async def test_same_bytes_failure_preserves_current_publication_file(
    artifact_override_service,
    seeded_event,
    session_factory,
    tmp_path,
) -> None:
    first = await artifact_override_service.override(
        actor_id="admin-id",
        event_id=seeded_event.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        revision_id=seeded_event.current_revision_id,
        reason="首次覆盖",
        expected_current_artifact_id=None,
        idempotency_key="00000000-0000-0000-0000-000000000007",
        upload=io.BytesIO(_VALID_JPEG_BYTES),
    )

    async with session_factory() as session:
        artifact = await session.get(GeneratedArtifact, first.artifact_id)
        assert artifact is not None
        storage_path = artifact.storage_path

    stored_path = ArtifactStore(
        tmp_path / "artifacts",
        max_override_bytes=1024 * 1024,
    ).resolve(storage_path)
    assert stored_path.exists()

    with pytest.raises(IdempotencyConflictError):
        await artifact_override_service.override(
            actor_id="admin-id",
            event_id=seeded_event.id,
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            revision_id=seeded_event.current_revision_id,
            reason="相同字节但错误预期",
            expected_current_artifact_id=uuid.uuid4(),
            idempotency_key="00000000-0000-0000-0000-000000000008",
            upload=io.BytesIO(_VALID_JPEG_BYTES),
        )

    assert stored_path.exists()
    async with session_factory() as session:
        current = await session.scalar(
            select(ArtifactPublication).where(
                ArtifactPublication.event_id == seeded_event.id,
                ArtifactPublication.artifact_key == "map.epicenter",
                ArtifactPublication.output_profile == "a3v-professional",
                ArtifactPublication.production_mode == "live",
                ArtifactPublication.superseded_at.is_(None),
            )
        )
    assert current is not None
    assert current.artifact_id == first.artifact_id


async def test_stale_cleanup_waits_for_in_flight_same_checksum_publisher(
    seeded_artifact_assessment,
    seeded_event,
    session_factory,
    tmp_path,
) -> None:
    await seeded_artifact_assessment.published_artifact(
        "map.epicenter",
        version=1,
    )
    stale_cleanup_ready = asyncio.Event()
    allow_stale_cleanup = asyncio.Event()
    new_worker_staged = asyncio.Event()

    async def stale_cleanup_hook() -> None:
        stale_cleanup_ready.set()
        await allow_stale_cleanup.wait()

    async def new_worker_staged_hook() -> None:
        new_worker_staged.set()

    service = ArtifactOverrideService(
        session_factory=session_factory,
        storage_root=tmp_path / "artifacts",
        lease_seconds=2.0,
        poll_interval=0.01,
        after_stage_before_store_hook=new_worker_staged_hook,
        before_cleanup_reference_check_hook=stale_cleanup_hook,
    )

    async def stale_worker() -> bool:
        try:
            await service.override(
                actor_id="admin-id",
                event_id=seeded_event.id,
                artifact_key="map.epicenter",
                output_profile="a3v-professional",
                revision_id=seeded_event.current_revision_id,
                reason="stale same checksum",
                expected_current_artifact_id=uuid.uuid4(),
                idempotency_key="00000000-0000-0000-0000-000000000011",
                upload=io.BytesIO(_VALID_JPEG_BYTES),
            )
        except IdempotencyConflictError:
            return True
        return False

    async def new_worker() -> ArtifactOverrideResponse:
        return await service.override(
            actor_id="admin-id",
            event_id=seeded_event.id,
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            revision_id=seeded_event.current_revision_id,
            reason="new same checksum",
            expected_current_artifact_id=None,
            idempotency_key="00000000-0000-0000-0000-000000000012",
            upload=io.BytesIO(_VALID_JPEG_BYTES),
        )

    stale_task = asyncio.create_task(stale_worker())
    await asyncio.wait_for(stale_cleanup_ready.wait(), timeout=10)
    new_task = asyncio.create_task(new_worker())
    await asyncio.wait_for(new_worker_staged.wait(), timeout=10)
    allow_stale_cleanup.set()

    stale_conflicted = await asyncio.wait_for(stale_task, timeout=10)
    response = await asyncio.wait_for(new_task, timeout=10)
    assert stale_conflicted is True

    async with session_factory() as session:
        artifact = await session.get(GeneratedArtifact, response.artifact_id)
        assert artifact is not None
        storage_path = artifact.storage_path
        current = await session.scalar(
            select(ArtifactPublication).where(
                ArtifactPublication.event_id == seeded_event.id,
                ArtifactPublication.artifact_key == "map.epicenter",
                ArtifactPublication.output_profile == "a3v-professional",
                ArtifactPublication.production_mode == "live",
                ArtifactPublication.superseded_at.is_(None),
            )
        )

    stored_path = ArtifactStore(
        tmp_path / "artifacts",
        max_override_bytes=1024 * 1024,
    ).resolve(storage_path)
    assert stored_path.exists()
    assert current is not None
    assert current.artifact_id == response.artifact_id


async def test_expired_processing_with_committed_records_recovers_without_new_run(
    artifact_override_service,
    seeded_event,
    session_factory,
) -> None:
    idempotency_key = "00000000-0000-0000-0000-000000000009"
    first = await artifact_override_service.override(
        actor_id="admin-id",
        event_id=seeded_event.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        revision_id=seeded_event.current_revision_id,
        reason="已提交租约恢复",
        expected_current_artifact_id=None,
        idempotency_key=idempotency_key,
        upload=io.BytesIO(_VALID_JPEG_BYTES),
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            request = await session.scalar(
                select(ArtifactOverrideRequest).where(
                    ArtifactOverrideRequest.actor_id == "admin-id",
                    ArtifactOverrideRequest.endpoint
                    == ArtifactOverrideService.endpoint_for(
                        seeded_event.id,
                        "map.epicenter",
                        "a3v-professional",
                    ),
                    ArtifactOverrideRequest.idempotency_key == idempotency_key,
                )
            )
            assert request is not None
            request.status = "processing"
            request.lease_expires_at = now - timedelta(seconds=1)
            request.lease_generation = 1
            request.response_body = None

    recovered = await artifact_override_service.override(
        actor_id="admin-id",
        event_id=seeded_event.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        revision_id=seeded_event.current_revision_id,
        reason="已提交租约恢复",
        expected_current_artifact_id=None,
        idempotency_key=idempotency_key,
        upload=io.BytesIO(_VALID_JPEG_BYTES),
    )

    async with session_factory() as session:
        run_count = await session.scalar(
            select(func.count())
            .select_from(ProductionRun)
            .where(ProductionRun.event_id == seeded_event.id)
        )
        request = await session.scalar(
            select(ArtifactOverrideRequest).where(
                ArtifactOverrideRequest.actor_id == "admin-id",
                ArtifactOverrideRequest.endpoint
                == ArtifactOverrideService.endpoint_for(
                    seeded_event.id,
                    "map.epicenter",
                    "a3v-professional",
                ),
                ArtifactOverrideRequest.idempotency_key == idempotency_key,
            )
        )

    assert recovered.artifact_id == first.artifact_id
    assert recovered.production_run_id == first.production_run_id
    assert run_count == 1
    assert request is not None
    assert request.status == "succeeded"


async def test_expected_current_conflict_is_persisted_for_retry(
    artifact_override_service,
    seeded_artifact_assessment,
    seeded_event,
    session_factory,
) -> None:
    await seeded_artifact_assessment.published_artifact(
        "map.epicenter",
        version=1,
    )
    idempotency_key = "00000000-0000-0000-0000-000000000010"
    kwargs = {
        "actor_id": "admin-id",
        "event_id": seeded_event.id,
        "artifact_key": "map.epicenter",
        "output_profile": "a3v-professional",
        "revision_id": seeded_event.current_revision_id,
        "reason": "错误预期",
        "expected_current_artifact_id": uuid.uuid4(),
        "idempotency_key": idempotency_key,
        "upload": io.BytesIO(_VALID_JPEG_BYTES),
    }

    with pytest.raises(IdempotencyConflictError):
        await artifact_override_service.override(**kwargs)
    async with session_factory() as session:
        request = await session.scalar(
            select(ArtifactOverrideRequest).where(
                ArtifactOverrideRequest.actor_id == "admin-id",
                ArtifactOverrideRequest.endpoint
                == ArtifactOverrideService.endpoint_for(
                    seeded_event.id,
                    "map.epicenter",
                    "a3v-professional",
                ),
                ArtifactOverrideRequest.idempotency_key == idempotency_key,
            )
        )
    assert request is not None
    assert request.status == "failed"
    assert request.response_body["error_type"] == "IdempotencyConflictError"

    with pytest.raises(IdempotencyConflictError):
        await artifact_override_service.override(**kwargs)


async def test_expired_processing_request_is_recovered(
    artifact_override_service,
    seeded_event,
    session_factory,
) -> None:
    artifact_key = "map.epicenter"
    output_profile = "a3v-professional"
    idempotency_key = "00000000-0000-0000-0000-000000000004"
    file_name = "map.epicenter.jpg"
    request_fingerprint = sha256_json(
        {
            "event_id": str(seeded_event.id),
            "revision_id": str(seeded_event.current_revision_id),
            "artifact_key": artifact_key,
            "output_profile": output_profile,
            "expected_current_artifact_id": None,
            "file_name": file_name,
            "size_bytes": _VALID_JPEG_SIZE,
            "checksum": _VALID_JPEG_CHECKSUM,
            "reason": "过期租约恢复",
        }
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                ArtifactOverrideRequest(
                    actor_id="admin-id",
                    endpoint=ArtifactOverrideService.endpoint_for(
                        seeded_event.id,
                        artifact_key,
                        output_profile,
                    ),
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    status="processing",
                    claimed_at=now - timedelta(seconds=10),
                    lease_expires_at=now - timedelta(seconds=1),
                    lease_generation=1,
                    attempt_count=1,
                )
            )

    response = await artifact_override_service.override(
        actor_id="admin-id",
        event_id=seeded_event.id,
        artifact_key=artifact_key,
        output_profile=output_profile,
        revision_id=seeded_event.current_revision_id,
        reason="过期租约恢复",
        expected_current_artifact_id=None,
        idempotency_key=idempotency_key,
        upload=io.BytesIO(_VALID_JPEG_BYTES),
    )

    async with session_factory() as session:
        request = await session.scalar(
            select(ArtifactOverrideRequest).where(
                ArtifactOverrideRequest.actor_id == "admin-id",
                ArtifactOverrideRequest.endpoint
                == ArtifactOverrideService.endpoint_for(
                    seeded_event.id,
                    artifact_key,
                    output_profile,
                ),
                ArtifactOverrideRequest.idempotency_key == idempotency_key,
            )
        )

    assert response.production_run_id is not None
    assert request is not None
    assert request.status == "succeeded"
    assert request.event_id == seeded_event.id
    assert request.lease_generation == 2


async def test_validation_failure_marks_request_failed_and_cleans_staging(
    artifact_override_service,
    seeded_event,
    session_factory,
    tmp_path,
) -> None:
    with pytest.raises(ArtifactOverrideValidationError):
        await artifact_override_service.override(
            actor_id="admin-id",
            event_id=seeded_event.id,
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            revision_id=seeded_event.current_revision_id,
            reason="无效文件",
            expected_current_artifact_id=None,
            idempotency_key="00000000-0000-0000-0000-000000000005",
            upload=io.BytesIO(b"not-an-image"),
        )

    async with session_factory() as session:
        endpoint = ArtifactOverrideService.endpoint_for(
            seeded_event.id,
            "map.epicenter",
            "a3v-professional",
        )
        request = await session.scalar(
            select(ArtifactOverrideRequest).where(
                ArtifactOverrideRequest.actor_id == "admin-id",
                ArtifactOverrideRequest.endpoint == endpoint,
                ArtifactOverrideRequest.idempotency_key == "00000000-0000-0000-0000-000000000005",
            )
        )
        run_count = await session.scalar(
            select(func.count())
            .select_from(ProductionRun)
            .where(ProductionRun.event_id == seeded_event.id)
        )

    assert request is not None
    assert request.status == "failed"
    assert request.event_id == seeded_event.id
    assert request.response_body is not None
    assert request.response_body["error_category"] == "format_mismatch"
    assert run_count == 0
    staging = tmp_path / "artifacts" / "staging"
    assert staging.exists()
    assert list(staging.iterdir()) == []
