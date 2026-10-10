from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.main import app
from app.operations.repository import StaleOperationsSettings
from app.operations.router import get_operations_service
from app.operations.schemas import (
    OperationsOverviewRecord,
    OperationsRunRecord,
    OperationsScheduleRecord,
    OperationsSettingsRecord,
    OperationsTriggerResult,
)
from app.operations.scenario import OperationsConfigurationError
from app.operations.service import RealEventPriorityActive


class FakeOperationsService:
    def __init__(self) -> None:
        self.triggered_keys: list[str | None] = []
        self.settings_version: int = 1

    async def trigger_manual_test(self, request, actor):
        del actor
        key = request.idempotency_key or str(uuid4())
        self.triggered_keys.append(request.idempotency_key)
        return OperationsTriggerResult(
            event_id=UUID("00000000-0000-4000-8000-000000000031"),
            run_id=UUID("00000000-0000-4000-8000-000000000032"),
            operation_state="queued",
            outcome="triggered",
            trigger_mode="manual_test",
            idempotency_key=key,
            event_kind="test",
        )

    async def trigger_drill(self, request, actor):
        del actor
        key = request.idempotency_key or str(uuid4())
        self.triggered_keys.append(request.idempotency_key)
        return OperationsTriggerResult(
            event_id=UUID("00000000-0000-4000-8000-000000000033"),
            run_id=UUID("00000000-0000-4000-8000-000000000034"),
            operation_state="queued",
            outcome="triggered",
            trigger_mode="drill",
            idempotency_key=key,
            event_kind="drill",
        )

    async def get_overview(self, observed_at):
        del observed_at
        return OperationsOverviewRecord(
            observed_at=datetime.now(UTC),
            real_event_priority_active=False,
            settings=_settings_record(),
            next_schedule=None,
            today_schedule=None,
            recent_runs=[],
            active_run_count=0,
        )

    async def get_settings(self):
        return _settings_record()

    async def update_settings(self, request, actor, expected_version):
        del request, actor
        self.settings_version = expected_version + 1
        return _settings_record(row_version=self.settings_version)

    async def list_schedules(self, limit):
        del limit
        return [
            OperationsScheduleRecord(
                id=UUID("00000000-0000-4000-8000-000000000035"),
                local_date=date(2040, 1, 15),
                window_start=datetime(2040, 1, 14, 18, tzinfo=UTC),
                window_end=datetime(2040, 1, 14, 20, tzinfo=UTC),
                scheduled_at=datetime(2040, 1, 14, 19, tzinfo=UTC),
                state="scheduled",
                random_seed=123,
                event_id=None,
                failure_reason=None,
                triggered_at=None,
                created_at=datetime(2040, 1, 14, 18, tzinfo=UTC),
                updated_at=datetime(2040, 1, 14, 18, tzinfo=UTC),
                scenario=None,
            )
        ]

    async def list_runs(self, limit):
        del limit
        return [
            OperationsRunRecord(
                id=UUID("00000000-0000-4000-8000-000000000036"),
                event_id=UUID("00000000-0000-4000-8000-000000000031"),
                schedule_id=None,
                trigger_mode="manual_test",
                operation_state="queued",
                priority=100,
                idempotency_key="api-key",
                triggered_by="operator",
                resume_required=False,
                detail={},
                started_at=None,
                paused_at=None,
                completed_at=None,
                canceled_at=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        ]


def _settings_record(*, row_version: int = 1) -> OperationsSettingsRecord:
    return OperationsSettingsRecord(
        row_version=row_version,
        daily_test_enabled=True,
        timezone="Asia/Shanghai",
        daily_window_start=time(2, 0),
        daily_window_end=time(4, 0),
        test_magnitude_min=Decimal("5.0"),
        test_magnitude_max=Decimal("5.9"),
        test_depth_min_km=Decimal("5.00"),
        test_depth_max_km=Decimal("20.00"),
        drill_conflict_policy="pause",
        updated_by="operator",
        updated_at=datetime.now(UTC),
    )


@contextmanager
def _client(
    *,
    service: FakeOperationsService | None = None,
    current_user: AuthUser | None = AuthUser("operator", "group_leader", None),
) -> Iterator[TestClient]:
    previous_service = app.dependency_overrides.get(get_operations_service)
    previous_user = app.dependency_overrides.get(get_current_user)
    if service is None:
        app.dependency_overrides.pop(get_operations_service, None)
    else:
        app.dependency_overrides[get_operations_service] = lambda: service
    if current_user is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = lambda: current_user
    try:
        yield TestClient(app)
    finally:
        if previous_service is None:
            app.dependency_overrides.pop(get_operations_service, None)
        else:
            app.dependency_overrides[get_operations_service] = previous_service
        if previous_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_user


def test_viewer_endpoints_require_authentication() -> None:
    with _client(current_user=None) as client:
        overview = client.get("/api/v1/operations/overview")
        settings = client.get("/api/v1/operations/settings")
        schedules = client.get("/api/v1/operations/schedules")
        runs = client.get("/api/v1/operations/runs")

    assert overview.status_code == 401
    assert settings.status_code == 401
    assert schedules.status_code == 401
    assert runs.status_code == 401


@pytest.mark.parametrize("role", ["viewer", "group_member"])
def test_viewer_can_read_but_cannot_trigger(role: str) -> None:
    service = FakeOperationsService()
    with _client(
        service=service,
        current_user=AuthUser(f"{role}-user", role, None),
    ) as client:
        overview = client.get("/api/v1/operations/overview")
        test_trigger = client.post("/api/v1/operations/test-trigger")
        drill_trigger = client.post("/api/v1/operations/drill-trigger")
        update = client.put(
            "/api/v1/operations/settings",
            json={"daily_test_enabled": False},
        )

    assert overview.status_code == 200
    assert test_trigger.status_code == 403
    assert drill_trigger.status_code == 403
    assert update.status_code == 403
    assert service.triggered_keys == []


def test_leader_can_trigger_with_idempotency_header() -> None:
    service = FakeOperationsService()
    with _client(
        service=service,
        current_user=AuthUser("leader", "group_leader", None),
    ) as client:
        response = client.post(
            "/api/v1/operations/test-trigger",
            headers={"Idempotency-Key": "api-test-key"},
        )

    assert response.status_code == 201
    assert response.json()["idempotency_key"] == "api-test-key"
    assert response.json()["operation_state"] == "queued"
    assert service.triggered_keys == ["api-test-key"]


def test_missing_header_generates_and_returns_stable_key() -> None:
    service = FakeOperationsService()
    with _client(service=service) as client:
        response = client.post("/api/v1/operations/drill-trigger", json={})

    assert response.status_code == 201
    assert response.json()["idempotency_key"]
    assert response.json()["idempotency_key"] == response.json()["idempotency_key"]
    assert service.triggered_keys == [None]


def test_superadmin_can_update_settings() -> None:
    service = FakeOperationsService()
    with _client(
        service=service,
        current_user=AuthUser("superadmin", "superadmin", None),
    ) as client:
        response = client.put(
            "/api/v1/operations/settings",
            json={"daily_test_enabled": False, "expected_version": 1},
        )

    assert response.status_code == 200
    assert response.json()["row_version"] == 2


def test_trigger_error_mapping() -> None:
    class ErrorService(FakeOperationsService):
        def __init__(self, error: Exception) -> None:
            super().__init__()
            self.error = error

        async def trigger_manual_test(self, request, actor):
            del request, actor
            raise self.error

    cases = [
        (RealEventPriorityActive(), 409, "real_event_priority_active"),
        (
            OperationsConfigurationError("active_shanghai_boundary_required"),
            503,
            "operations_configuration_unavailable",
        ),
        (LookupError("schedule_not_found"), 404, "schedule_not_found"),
        (ValueError("bad input"), 422, "bad input"),
        (SQLAlchemyError("db down"), 503, "operations_storage_unavailable"),
    ]
    for error, status, detail in cases:
        with _client(service=ErrorService(error)) as client:
            response = client.post("/api/v1/operations/test-trigger")
        assert response.status_code == status
        assert response.json()["detail"] == detail


def test_settings_stale_error_mapping() -> None:
    class ErrorService(FakeOperationsService):
        async def update_settings(self, request, actor, expected_version):
            del request, actor, expected_version
            raise StaleOperationsSettings(1, 2)

    with _client(
        service=ErrorService(),
        current_user=AuthUser("superadmin", "superadmin", None),
    ) as client:
        response = client.put(
            "/api/v1/operations/settings",
            json={"daily_test_enabled": False, "expected_version": 1},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "settings_version_conflict"


def test_list_endpoints_return_200() -> None:
    with _client(service=FakeOperationsService()) as client:
        schedules = client.get("/api/v1/operations/schedules")
        runs = client.get("/api/v1/operations/runs")

    assert schedules.status_code == 200
    assert schedules.json()[0]["id"] == "00000000-0000-4000-8000-000000000035"
    assert runs.status_code == 200
    assert runs.json()[0]["operation_state"] == "queued"
