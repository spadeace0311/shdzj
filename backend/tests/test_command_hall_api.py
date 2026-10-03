import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.command_hall.projector import CommandHallProjector
from app.command_hall.router import (
    get_command_hall_service,
    get_command_hall_session,
)
from app.command_hall.service import CommandHallService
from app.collaboration.domain import WorkgroupCode
from app.collaboration.models import WorkgroupTask
from app.config import settings
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage
from app.main import app


class _SyncASGITransport(httpx.ASGITransport):
    def handle_request(self, request):
        async def send():
            response = await self.handle_async_request(request)
            content = b"".join(
                [chunk async for chunk in response.aiter_bytes()]
            )
            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=content,
                request=request,
            )

        return asyncio.run(send())

    def close(self) -> None:
        return None


class _UnknownEventCommandHallService:
    async def get_event_projection(self, session, event_id):
        del session, event_id
        return None


@pytest.fixture
async def session():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            transaction = await session.begin()
            try:
                yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def event_factory(session):
    async def factory(*, event_type: str = "formal") -> EarthquakeEvent:
        event = EarthquakeEvent(
            id=uuid.uuid4(),
            source="command-hall-api-test",
            canonical_source_id=f"hall-api-{uuid.uuid4().hex}",
            event_type=event_type,
            origin_time=datetime(2026, 10, 3, 0, 0, tzinfo=UTC),
            longitude=Decimal("121.500000"),
            latitude=Decimal("31.200000"),
            depth_km=Decimal("10.00"),
            magnitude=Decimal("5.2"),
            place="command hall api fixture",
            geom=WKTElement("POINT(121.5 31.2)", srid=4326),
            lifecycle_state="formal_triggered",
        )
        session.add(event)
        await session.flush()
        return event

    return factory


@pytest.fixture
def revision_factory(session):
    async def factory(event: EarthquakeEvent) -> EarthquakeRevision:
        raw = RawMessage(
            id=uuid.uuid4(),
            source="command-hall-api-test",
            source_message_id=uuid.uuid4().hex,
            message_kind=event.event_type,
            checksum=uuid.uuid4().hex,
            payload={"event_id": str(event.id)},
        )
        session.add(raw)
        await session.flush()
        revision = EarthquakeRevision(
            id=uuid.uuid4(),
            event_id=event.id,
            raw_message_id=raw.id,
            revision_no=1,
            revision_kind=event.event_type,
            origin_time=event.origin_time,
            longitude=event.longitude,
            latitude=event.latitude,
            depth_km=event.depth_km,
            magnitude=event.magnitude,
            place=event.place,
            is_current=True,
            ingested_at=event.origin_time,
        )
        session.add(revision)
        await session.flush()
        return revision

    return factory


@pytest.fixture
def task_factory(session, revision_factory):
    async def factory(
        event: EarthquakeEvent,
        *,
        workgroup_code: str = WorkgroupCode.MONITORING_FORECAST.value,
        status: str = "pending",
        timeliness_state: str = "on_time",
    ) -> WorkgroupTask:
        revision = await session.scalar(
            select(EarthquakeRevision)
            .where(EarthquakeRevision.event_id == event.id)
            .order_by(EarthquakeRevision.revision_no)
            .limit(1)
        )
        if revision is None:
            revision = await revision_factory(event)
        now = datetime.now(UTC)
        task = WorkgroupTask(
            event_id=event.id,
            trigger_revision_id=revision.id,
            task_code=f"api-task-{uuid.uuid4().hex}",
            source_type="ad_hoc",
            workgroup_code=workgroup_code,
            title="Command hall API task",
            instruction="Exercise command hall reads.",
            priority=100,
            status=status,
            timeliness_state=timeliness_state,
            phase_code="within_30m",
            activated_at=now,
            due_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.flush()
        return task

    return factory


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="hall-reader",
        role="viewer",
        workgroup=None,
    )
    async def override_session():
        yield None

    app.dependency_overrides[get_command_hall_session] = override_session
    app.dependency_overrides[get_command_hall_service] = (
        lambda: _UnknownEventCommandHallService()
    )
    test_client = httpx.Client(
        transport=_SyncASGITransport(app=app),
        base_url="http://test",
    )
    try:
        yield test_client
    finally:
        test_client.close()
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_command_hall_session, None)
        app.dependency_overrides.pop(get_command_hall_service, None)


def test_sse_rejects_unknown_event(client):
    response = client.get(
        "/api/v1/command-hall/events/00000000-0000-0000-0000-000000000000/stream"
    )
    assert response.status_code == 404


def test_command_hall_requires_authentication():
    test_client = httpx.Client(
        transport=_SyncASGITransport(app=app),
        base_url="http://test",
    )
    try:
        response = test_client.get("/api/v1/command-hall/active-event")
    finally:
        test_client.close()
    assert response.status_code == 401


async def test_command_hall_read_routes(
    session,
    event_factory,
    revision_factory,
    task_factory,
):
    event = await event_factory()
    await revision_factory(event)
    task = await task_factory(event)
    await CommandHallProjector().refresh_event(session, event.id)

    async def override_session():
        yield session

    app.dependency_overrides[get_command_hall_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="hall-reader",
        role="viewer",
        workgroup=None,
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as test_client:
            active = await test_client.get(
                "/api/v1/command-hall/active-event"
            )
            overview = await test_client.get(
                f"/api/v1/command-hall/events/{event.id}/overview"
            )
            group = await test_client.get(
                f"/api/v1/command-hall/events/{event.id}/groups/"
                "monitoring_forecast"
            )
            detail = await test_client.get(
                f"/api/v1/command-hall/tasks/{task.id}"
            )
    finally:
        app.dependency_overrides.pop(get_command_hall_session, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert active.status_code == 200
    assert active.json()["event_id"] == str(event.id)
    assert overview.status_code == 200
    assert overview.json()["group_count"] == 7
    assert overview.json()["task_counts"]["pending"] == 1
    assert group.status_code == 200
    assert group.json()["group"]["workgroup_code"] == "monitoring_forecast"
    assert detail.status_code == 200
    assert detail.json()["id"] == str(task.id)


async def test_projection_stream_emits_change_heartbeat_and_stops_on_disconnect():
    service = CommandHallService()
    state = {"version": 1, "disconnect": False}
    now_value = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)

    async def read_projection():
        return SimpleNamespace(projection_version=state["version"])

    async def is_disconnected():
        return state["disconnect"]

    async def fake_sleep(_seconds):
        return None

    stream = service.stream_projection(
        uuid.uuid4(),
        read_projection=read_projection,
        is_disconnected=is_disconnected,
        poll_seconds=1,
        heartbeat_seconds=0,
        sleep=fake_sleep,
        now=lambda: now_value,
    )

    first = await anext(stream)
    assert first.startswith(": heartbeat")

    state["version"] = 2
    second = await anext(stream)
    assert "event: projection.updated" in second
    assert '"projection_version":2' in second

    state["disconnect"] = True
    await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
