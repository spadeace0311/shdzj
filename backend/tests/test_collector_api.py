from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.collector.router import get_collector_status_service
from app.main import app


class FakeCollectorStatusService:
    async def get_status(self) -> dict[str, object]:
        return {
            "overall_state": "healthy",
            "providers": [
                {
                    "provider": "fan",
                    "state": "healthy",
                    "connected": True,
                    "last_http_status": None,
                    "last_connected_at": "2026-09-25T01:05:00Z",
                    "last_message_at": "2026-09-25T01:05:10Z",
                    "last_success_at": "2026-09-25T01:05:10Z",
                    "consecutive_failures": 0,
                    "reconnect_count": 0,
                    "last_error": None,
                    "updated_at": "2026-09-25T01:05:10Z",
                },
                {
                    "provider": "wolfx",
                    "state": "healthy",
                    "connected": True,
                    "last_http_status": 200,
                    "last_connected_at": "2026-09-25T01:05:00Z",
                    "last_message_at": "2026-09-25T01:05:10Z",
                    "last_success_at": "2026-09-25T01:05:10Z",
                    "consecutive_failures": 0,
                    "reconnect_count": 0,
                    "last_error": None,
                    "updated_at": "2026-09-25T01:05:10Z",
                },
            ],
            "open_dead_letter_count": 0,
            "boundary_version": "test-2026.1",
            "last_ingested_event_id": "event-1",
        }


_MISSING = object()


@contextmanager
def _client(*, current_user: AuthUser | None = None) -> Iterator[TestClient]:
    overrides = {
        get_current_user: current_user,
        get_collector_status_service: FakeCollectorStatusService(),
    }
    previous: dict[object, object] = {}
    for dependency, value in overrides.items():
        previous[dependency] = app.dependency_overrides.get(dependency, _MISSING)
        app.dependency_overrides[dependency] = lambda value=value: value
    try:
        yield TestClient(app)
    finally:
        for dependency, value in previous.items():
            if value is _MISSING:
                app.dependency_overrides.pop(dependency, None)
            else:
                app.dependency_overrides[dependency] = value


def test_collector_status_requires_authentication() -> None:
    response = TestClient(app).get("/api/v1/collector/status")

    assert response.status_code == 401


def test_collector_status_returns_both_providers() -> None:
    current_user = AuthUser(
        username="superadmin",
        role="superadmin",
        workgroup=None,
    )

    with _client(current_user=current_user) as client:
        response = client.get("/api/v1/collector/status")

    assert response.status_code == 200
    assert response.json()["overall_state"] == "healthy"
    assert {item["provider"] for item in response.json()["providers"]} == {"fan", "wolfx"}


def test_collector_status_rejects_role_without_permission() -> None:
    current_user = AuthUser(username="viewer", role="viewer", workgroup=None)

    with _client(current_user=current_user) as client:
        response = client.get("/api/v1/collector/status")

    assert response.status_code == 403


@pytest.mark.parametrize("role", ("group_leader", "group_deputy"))
def test_collector_status_allows_group_roles(role: str) -> None:
    current_user = AuthUser(username=f"{role}-user", role=role, workgroup="sh")

    with _client(current_user=current_user) as client:
        response = client.get("/api/v1/collector/status")

    assert response.status_code == 200
