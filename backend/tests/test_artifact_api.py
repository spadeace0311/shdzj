from __future__ import annotations

import io
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.artifacts.models import ProductionRun
from app.artifacts.router import _workflow_input
from app.artifacts.storage import ArtifactStore
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.config import settings
from app.db import engine
from app.main import app


class RecordingArtifactWorkflowStarter:
    def __init__(self, *, raise_on_start: bool = False) -> None:
        self.requests = []
        self.raise_on_start = raise_on_start

    async def start(self, request: object) -> None:
        if self.raise_on_start:
            raise OSError("temporal unavailable")
        self.requests.append(request)


def _jpeg_bytes(color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    image = Image.new("RGB", (4761, 3369), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 1200, 1200), fill=color)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=35, dpi=(300, 300))
    return output.getvalue()


def test_metadata_rebuild_workflow_input_passes_render_concurrency(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "artifact_render_concurrency", 3)
    run = SimpleNamespace(
        id=uuid4(),
        assessment_run_id=uuid4(),
        event_id=uuid4(),
        revision_id=uuid4(),
        deadline_at=datetime(2026, 9, 26, 3, 8, tzinfo=UTC),
        deadline_basis_at=datetime(2026, 9, 26, 3, 3, tzinfo=UTC),
        catalog_version="catalog-v1",
        context_fingerprint="",
        launch_mode="standalone",
        generation_seq=2,
        generation_scope="artifact:map.epicenter:a3v-professional",
        required_outputs=[
            {
                "artifact_key": "map.epicenter",
                "output_profile": "a3v-professional",
            }
        ],
    )

    request = _workflow_input(run)

    assert request.render_concurrency == 3


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def artifact_client():
    from app.artifacts.router import get_artifact_workflow_starter

    starter = RecordingArtifactWorkflowStarter()
    previous_user = app.dependency_overrides.get(get_current_user)
    previous_starter = app.dependency_overrides.get(get_artifact_workflow_starter)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="artifact-admin",
        role="superadmin",
        workgroup=None,
    )
    app.dependency_overrides[get_artifact_workflow_starter] = lambda: starter
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client
    finally:
        if previous_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_user
        if previous_starter is None:
            app.dependency_overrides.pop(get_artifact_workflow_starter, None)
        else:
            app.dependency_overrides[get_artifact_workflow_starter] = previous_starter


@pytest.fixture
async def artifact_client_with_failing_starter():
    from app.artifacts.router import get_artifact_workflow_starter

    starter = RecordingArtifactWorkflowStarter(raise_on_start=True)
    previous_user = app.dependency_overrides.get(get_current_user)
    previous_starter = app.dependency_overrides.get(get_artifact_workflow_starter)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="artifact-admin",
        role="superadmin",
        workgroup=None,
    )
    app.dependency_overrides[get_artifact_workflow_starter] = lambda: starter
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client
    finally:
        if previous_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_user
        if previous_starter is None:
            app.dependency_overrides.pop(get_artifact_workflow_starter, None)
        else:
            app.dependency_overrides[get_artifact_workflow_starter] = previous_starter


@pytest.fixture
async def artifact_client_as_group_member():
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="artifact-member",
        role="group_member",
        workgroup="综合协调组",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


async def test_artifact_read_api_returns_thumbnail_and_download_urls(
    artifact_client,
    seeded_artifact_assessment,
) -> None:
    event = await seeded_artifact_assessment.publish_epicenter_artifact()
    response = await artifact_client.get(f"/api/v1/events/{event.event_id}/artifacts")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["artifact_key"] == "map.epicenter"
    assert body[0]["thumbnail_url"].endswith("/thumbnail")
    assert body[0]["download_url"].endswith("/download")


async def test_rebuild_returns_202_and_does_not_change_full_progress(
    artifact_client,
    seeded_artifact_assessment,
) -> None:
    event = await seeded_artifact_assessment.publish_full_progress()
    response = await artifact_client.post(
        f"/api/v1/events/{event.event_id}/artifacts/map.epicenter/rebuild"
        "?output_profile=a3v-professional",
        json={"reason": "底图版本更新后重新生成"},
    )

    assert response.status_code == 202
    assert response.json()["generation_scope"] == (
        "artifact:map.epicenter:a3v-professional"
    )
    assert await seeded_artifact_assessment.full_progress(event.event_id) == "39/39"


async def test_override_requires_superadmin(
    artifact_client_as_group_member,
    seeded_artifact_assessment,
) -> None:
    response = await artifact_client_as_group_member.post(
        f"/api/v1/events/{seeded_artifact_assessment.event.id}/artifacts/map.epicenter/override",
        files={"file": ("map.jpg", b"not-a-jpeg", "image/jpeg")},
        data={
            "revision_id": str(seeded_artifact_assessment.revision.id),
            "reason": "test",
        },
        headers={"Idempotency-Key": "00000000-0000-0000-0000-000000000003"},
    )

    assert response.status_code == 403


async def test_rebuild_without_full_run_returns_409(
    artifact_client,
    seeded_artifact_assessment,
    session_factory,
) -> None:
    response = await artifact_client.post(
        f"/api/v1/events/{seeded_artifact_assessment.event.id}"
        "/artifacts/map.epicenter/rebuild?output_profile=a3v-professional",
        json={"reason": "底图版本更新后重新生成"},
    )

    assert response.status_code == 409
    async with session_factory() as session:
        run_count = len(
            (
                await session.scalars(
                    select(ProductionRun).where(
                        ProductionRun.event_id
                        == seeded_artifact_assessment.event_id
                    )
                )
            ).all()
        )
    assert run_count == 0


async def test_rebuild_workflow_start_failure_returns_503_and_terminal_run(
    artifact_client_with_failing_starter,
    seeded_artifact_assessment,
    session_factory,
) -> None:
    full_run = await seeded_artifact_assessment.publish_full_progress()
    response = await artifact_client_with_failing_starter.post(
        f"/api/v1/events/{full_run.event_id}/artifacts/map.epicenter/rebuild"
        "?output_profile=a3v-professional",
        json={"reason": "底图版本更新后重新生成"},
    )

    assert response.status_code == 503
    async with session_factory() as session:
        run = await session.scalar(
            select(ProductionRun)
            .where(
                ProductionRun.event_id == full_run.event_id,
                ProductionRun.generation_scope
                == "artifact:map.epicenter:a3v-professional",
            )
            .order_by(ProductionRun.created_at.desc())
            .limit(1)
        )
    assert run is not None
    assert run.status == "failed"
    assert "workflow_start_failed" in (run.last_error or "")


async def test_rebuild_persists_audit_snapshot(
    artifact_client,
    seeded_artifact_assessment,
    session_factory,
) -> None:
    published = await seeded_artifact_assessment.publish_epicenter_artifact()
    response = await artifact_client.post(
        f"/api/v1/events/{published.event_id}/artifacts/map.epicenter/rebuild"
        "?output_profile=a3v-professional",
        json={"reason": "底图版本更新后重新生成"},
    )

    assert response.status_code == 202
    async with session_factory() as session:
        run = await session.scalar(
            select(ProductionRun)
            .where(
                ProductionRun.event_id == published.event_id,
                ProductionRun.generation_scope
                == "artifact:map.epicenter:a3v-professional",
            )
            .order_by(ProductionRun.created_at.desc())
            .limit(1)
        )
    assert run is not None
    snapshot = run.snapshot or {}
    assert snapshot["operator"] == "artifact-admin"
    assert "rebuild_requested_at" in snapshot
    assert snapshot["artifact_key"] == "map.epicenter"
    assert snapshot["output_profile"] == "a3v-professional"
    assert snapshot["old_current_artifact_id"] == str(published.id)
    assert snapshot["new_production_run_id"] == response.json()["production_run_id"]
    assert snapshot["reason"] == "底图版本更新后重新生成"


async def test_download_and_thumbnail_stream_registered_file(
    artifact_client,
    seeded_artifact_assessment,
) -> None:
    artifact = await seeded_artifact_assessment.generated_artifact(
        "map.epicenter",
        version=1,
    )
    path = ArtifactStore(settings.artifact_storage_root).resolve(
        artifact.storage_path
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"stream-bytes")

    download = await artifact_client.get(
        f"/api/v1/artifacts/{artifact.id}/download"
    )
    thumbnail = await artifact_client.get(
        f"/api/v1/artifacts/{artifact.id}/thumbnail"
    )

    assert download.status_code == 200
    assert download.content == b"stream-bytes"
    assert download.headers["content-type"].startswith("image/jpeg")
    assert thumbnail.status_code == 200
    assert thumbnail.content == b"stream-bytes"
    assert thumbnail.headers["content-type"].startswith("image/jpeg")


async def test_download_rejects_invalid_managed_path(
    artifact_client,
    seeded_artifact_assessment,
    session_factory,
) -> None:
    artifact = await seeded_artifact_assessment.generated_artifact(
        "map.epicenter",
        version=1,
    )
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(type(artifact), artifact.id)
            assert row is not None
            row.storage_path = "../escaped-artifact.jpg"

    response = await artifact_client.get(
        f"/api/v1/artifacts/{artifact.id}/download"
    )

    assert response.status_code == 503


async def test_override_success_and_idempotent_retry(
    artifact_client,
    seeded_artifact_assessment,
) -> None:
    jpeg = _jpeg_bytes()
    idempotency_key = "10000000-0000-0000-0000-000000000001"

    async def post_override() -> object:
        return await artifact_client.post(
            f"/api/v1/events/{seeded_artifact_assessment.event.id}"
            "/artifacts/map.epicenter/override?output_profile=a3v-professional",
            files={"file": ("map.jpg", jpeg, "image/jpeg")},
            data={
                "revision_id": str(seeded_artifact_assessment.revision.id),
                "reason": "替换错误图件",
            },
            headers={"Idempotency-Key": idempotency_key},
        )

    first = await post_override()
    second = await post_override()

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["artifact_id"] == first.json()["artifact_id"]
    assert second.json()["production_run_id"] == first.json()["production_run_id"]


async def test_override_invalid_upload_returns_422(
    artifact_client,
    seeded_artifact_assessment,
) -> None:
    response = await artifact_client.post(
        f"/api/v1/events/{seeded_artifact_assessment.event.id}"
        "/artifacts/map.epicenter/override?output_profile=a3v-professional",
        files={"file": ("map.jpg", b"not-a-jpeg", "image/jpeg")},
        data={
            "revision_id": str(seeded_artifact_assessment.revision.id),
            "reason": "test",
        },
        headers={"Idempotency-Key": "10000000-0000-0000-0000-000000000002"},
    )

    assert response.status_code == 422


async def test_override_missing_event_returns_404(
    artifact_client,
    seeded_artifact_assessment,
) -> None:
    response = await artifact_client.post(
        f"/api/v1/events/{uuid4()}/artifacts/map.epicenter/override"
        "?output_profile=a3v-professional",
        files={"file": ("map.jpg", _jpeg_bytes(), "image/jpeg")},
        data={
            "revision_id": str(seeded_artifact_assessment.revision.id),
            "reason": "test",
        },
        headers={"Idempotency-Key": "10000000-0000-0000-0000-000000000003"},
    )

    assert response.status_code == 404


async def test_override_conflict_returns_409(
    artifact_client,
    seeded_artifact_assessment,
) -> None:
    idempotency_key = "10000000-0000-0000-0000-000000000004"
    await artifact_client.post(
        f"/api/v1/events/{seeded_artifact_assessment.event.id}"
        "/artifacts/map.epicenter/override?output_profile=a3v-professional",
        files={"file": ("map.jpg", _jpeg_bytes((255, 0, 0)), "image/jpeg")},
        data={
            "revision_id": str(seeded_artifact_assessment.revision.id),
            "reason": "第一次原因",
        },
        headers={"Idempotency-Key": idempotency_key},
    )

    response = await artifact_client.post(
        f"/api/v1/events/{seeded_artifact_assessment.event.id}"
        "/artifacts/map.epicenter/override?output_profile=a3v-professional",
        files={"file": ("map.jpg", _jpeg_bytes((0, 0, 255)), "image/jpeg")},
        data={
            "revision_id": str(seeded_artifact_assessment.revision.id),
            "reason": "第二次原因",
        },
        headers={"Idempotency-Key": idempotency_key},
    )

    assert response.status_code == 409
