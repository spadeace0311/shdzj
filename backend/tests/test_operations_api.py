from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from geoalchemy2.elements import WKTElement
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.collaboration.models import CollaborationSettings
from app.db import SessionFactory, engine
from app.events.models import (
    EarthquakeEvent,
    EventLifecycleOutbox,
    RawMessage,
)
from app.main import app
from app.operations.models import (
    OperationsAuditLog,
    OperationsRun,
    OperationsSettings,
)
from app.operations.repository import (
    OperationsRepository,
    StaleOperationsSettings,
)
from app.operations.router import get_operations_service
from app.operations.schemas import (
    DrillTriggerRequest,
    OperationsOverviewRecord,
    OperationsRunRecord,
    OperationsScheduleRecord,
    OperationsSettingsRecord,
    OperationsTriggerResult,
)
from app.operations.scenario import OperationsConfigurationError
from app.operations.service import OperationsService, RealEventPriorityActive
from app.regions.models import RegionBoundary


API_OBSERVED_AT = datetime(2040, 1, 15, 4, 0, tzinfo=UTC)


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

    async def resume_drill(self, run_id, actor):
        del actor
        return OperationsTriggerResult(
            event_id=UUID("00000000-0000-4000-8000-000000000033"),
            run_id=UUID("00000000-0000-4000-8000-000000000034"),
            operation_state="queued",
            outcome="triggered",
            trigger_mode="drill",
            idempotency_key=str(uuid4()),
            event_kind="drill",
            resumed_from_run_id=UUID(str(run_id)),
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


async def _ensure_api_operations_environment() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            active_boundary = await session.scalar(
                select(RegionBoundary)
                .where(RegionBoundary.is_active.is_(True))
                .limit(1)
            )
            if active_boundary is None:
                session.add(
                    RegionBoundary(
                        version=f"api-test-boundary-{uuid4().hex}",
                        name="api test boundary",
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
                        source_uri="test://operations-api-boundary",
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
                        id=UUID("00000000-0000-0000-0000-000000000022"),
                        intensity_threshold=Decimal("2.0"),
                        row_version=1,
                    )
                )
            await OperationsRepository().get_settings(session)


async def _cleanup_api_operations_events() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            event_ids = list(
                await session.scalars(
                    select(EarthquakeEvent.id).where(
                        EarthquakeEvent.source == "operations"
                    )
                )
            )
            if not event_ids:
                return
            run_ids = list(
                await session.scalars(
                    select(OperationsRun.id).where(
                        OperationsRun.event_id.in_(event_ids)
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
                    OperationsRun.event_id.in_(event_ids)
                )
            )
            await session.execute(
                delete(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.event_id.in_(event_ids)
                )
            )
            await session.execute(
                delete(EarthquakeEvent).where(
                    EarthquakeEvent.id.in_(event_ids)
                )
            )
            await session.execute(
                delete(RawMessage).where(RawMessage.source == "operations")
            )


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


def test_drill_resume_requires_superadmin_and_returns_run_link() -> None:
    with _client(
        service=FakeOperationsService(),
        current_user=AuthUser("operator", "group_leader", None),
    ) as client:
        forbidden = client.post(
            "/api/v1/operations/runs/00000000-0000-4000-8000-000000000001/resume"
        )
    assert forbidden.status_code == 403

    with _client(
        service=FakeOperationsService(),
        current_user=AuthUser("admin", "superadmin", None),
    ) as client:
        resumed = client.post(
            "/api/v1/operations/runs/00000000-0000-4000-8000-000000000001/resume"
        )
    assert resumed.status_code == 201
    assert resumed.json()["resumed_from_run_id"] == (
        "00000000-0000-4000-8000-000000000001"
    )


async def test_repeated_drill_resume_posts_are_idempotent() -> None:
    await _ensure_api_operations_environment()
    service = OperationsService(
        SessionFactory,
        now=lambda: API_OBSERVED_AT,
    )
    source = await service.trigger_drill(
        DrillTriggerRequest(
            idempotency_key=f"api-resume-source-{uuid4()}"
        ),
        "operator",
    )
    assert source.run_id is not None

    async with SessionFactory() as session:
        async with session.begin():
            run = await session.get(OperationsRun, source.run_id)
            assert run is not None
            run.operation_state = "paused"
            run.resume_required = True
            run.paused_at = API_OBSERVED_AT

    previous_service = app.dependency_overrides.get(get_operations_service)
    previous_user = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_operations_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        "superadmin",
        "superadmin",
        None,
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            first = await client.post(
                f"/api/v1/operations/runs/{source.run_id}/resume"
            )
            second = await client.post(
                f"/api/v1/operations/runs/{source.run_id}/resume"
            )

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["run_id"] == second.json()["run_id"]
        assert first.json()["event_id"] == second.json()["event_id"]
        assert first.json()["idempotency_key"] == f"resume:{source.run_id}"

        async with SessionFactory() as session:
            old_run = await session.get(OperationsRun, source.run_id)

        assert old_run is not None
        assert old_run.operation_state == "canceled"
        assert old_run.resume_required is False
        assert old_run.canceled_at == API_OBSERVED_AT
        assert old_run.detail["resumed_by_run_id"] == first.json()["run_id"]
    finally:
        if previous_service is None:
            app.dependency_overrides.pop(get_operations_service, None)
        else:
            app.dependency_overrides[get_operations_service] = previous_service
        if previous_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_user
        await _cleanup_api_operations_events()
        await engine.dispose()


async def test_public_triggers_cannot_preempt_resume_namespace() -> None:
    await _ensure_api_operations_environment()
    service = OperationsService(
        SessionFactory,
        now=lambda: API_OBSERVED_AT,
    )
    source = await service.trigger_drill(
        DrillTriggerRequest(
            idempotency_key=f"api-resume-preempt-source-{uuid4()}"
        ),
        "operator",
    )
    assert source.run_id is not None
    resume_key = f"resume:{source.run_id}"

    async with SessionFactory() as session:
        async with session.begin():
            run = await session.get(OperationsRun, source.run_id)
            assert run is not None
            run.operation_state = "paused"
            run.resume_required = True
            run.paused_at = API_OBSERVED_AT

    previous_service = app.dependency_overrides.get(get_operations_service)
    previous_user = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_operations_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        "leader",
        "group_leader",
        None,
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            manual_preempt = await client.post(
                "/api/v1/operations/test-trigger",
                json={},
                headers={"Idempotency-Key": resume_key},
            )
            drill_preempt = await client.post(
                "/api/v1/operations/drill-trigger",
                json={},
                headers={"Idempotency-Key": resume_key},
            )

            app.dependency_overrides[get_current_user] = lambda: AuthUser(
                "superadmin",
                "superadmin",
                None,
            )
            resumed = await client.post(
                f"/api/v1/operations/runs/{source.run_id}/resume"
            )

        assert manual_preempt.status_code == 422
        assert drill_preempt.status_code == 422
        assert "reserved namespace" in manual_preempt.text
        assert "reserved namespace" in drill_preempt.text
        assert resumed.status_code == 201
        assert resumed.json()["idempotency_key"] == resume_key

        async with SessionFactory() as session:
            manual_preempt_count = await session.scalar(
                select(func.count())
                .select_from(OperationsRun)
                .where(
                    OperationsRun.idempotency_key == resume_key,
                    OperationsRun.trigger_mode == "manual_test",
                )
            )
            replacement = await session.get(
                OperationsRun,
                UUID(resumed.json()["run_id"]),
            )
            source_run = await session.get(OperationsRun, source.run_id)

        assert manual_preempt_count == 0
        assert replacement is not None
        assert replacement.trigger_mode == "drill"
        assert replacement.detail["resumed_from_source_run_id"] == str(
            source.run_id
        )
        assert source_run is not None
        assert source_run.detail["resumed_by_run_id"] == str(replacement.id)
    finally:
        if previous_service is None:
            app.dependency_overrides.pop(get_operations_service, None)
        else:
            app.dependency_overrides[get_operations_service] = previous_service
        if previous_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_user
        await _cleanup_api_operations_events()
        await engine.dispose()


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


def test_settings_reject_unknown_timezone_before_service_call() -> None:
    service = FakeOperationsService()
    with _client(
        service=service,
        current_user=AuthUser("admin", "superadmin", None),
    ) as client:
        response = client.put(
            "/api/v1/operations/settings",
            json={
                "expected_version": 1,
                "timezone": "Not/AZone",
            },
        )

    assert response.status_code == 422
    assert "invalid_timezone" in response.text
    assert service.settings_version == 1


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


async def test_superadmin_can_repair_invalid_operations_timezone() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(OperationsSettings))
    await _ensure_api_operations_environment()
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(OperationsSettings))
            session.add(
                OperationsSettings(
                    settings_key="default",
                    timezone="Not/AZone",
                    row_version=1,
                )
            )

    previous_service = app.dependency_overrides.get(get_operations_service)
    previous_user = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_operations_service] = lambda: OperationsService(
        SessionFactory,
        now=lambda: API_OBSERVED_AT,
    )
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        "superadmin",
        "superadmin",
        None,
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.put(
                "/api/v1/operations/settings",
                json={
                    "expected_version": 1,
                    "timezone": "Europe/London",
                },
            )

        assert response.status_code == 200
        assert response.json()["timezone"] == "Europe/London"
        assert response.json()["row_version"] == 2
        assert response.json()["updated_by"] == "superadmin"
    finally:
        if previous_service is None:
            app.dependency_overrides.pop(get_operations_service, None)
        else:
            app.dependency_overrides[get_operations_service] = previous_service
        if previous_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_user
        async with SessionFactory() as session:
            async with session.begin():
                await session.execute(delete(OperationsSettings))
                await OperationsRepository().get_settings(session)
        await engine.dispose()


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
