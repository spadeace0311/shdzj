from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.db import engine
from app.main import app


class RecordingArtifactWorkflowStarter:
    def __init__(self) -> None:
        self.requests = []

    async def start(self, request: object) -> None:
        self.requests.append(request)


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
