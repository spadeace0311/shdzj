from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import func, select

from app.artifacts.models import (
    ArtifactOverrideRequest,
    ArtifactPublication,
    GeneratedArtifact,
    ProductionRun,
)
from app.artifacts.service import (
    ArtifactOverrideResponse,
    ArtifactOverrideService,
    ArtifactOverrideValidationError,
    IdempotencyConflictError,
    sha256_json,
)
from app.artifacts.storage import ArtifactStore
from app.db import engine


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


_VALID_JPEG_BYTES = _jpeg_bytes((255, 0, 0))
_OTHER_JPEG_BYTES = _jpeg_bytes((0, 0, 255))
_VALID_JPEG_CHECKSUM = hashlib.sha256(_VALID_JPEG_BYTES).hexdigest()
_VALID_JPEG_SIZE = len(_VALID_JPEG_BYTES)


@dataclass(frozen=True, slots=True)
class EventReference:
    id: uuid.UUID
    current_revision_id: uuid.UUID


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
                ArtifactOverrideRequest.idempotency_key
                == "00000000-0000-0000-0000-000000000005"
            )
        )
        run_count = await session.scalar(
            select(func.count())
            .select_from(ProductionRun)
            .where(ProductionRun.event_id == seeded_event.id)
        )

    assert request is not None
    assert request.status == "failed"
    assert request.response_body is not None
    assert request.response_body["error_category"] == "format_mismatch"
    assert run_count == 0
    staging = tmp_path / "artifacts" / "staging"
    assert staging.exists()
    assert list(staging.iterdir()) == []
