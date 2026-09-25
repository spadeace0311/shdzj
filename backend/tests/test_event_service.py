import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy.dialects import postgresql

from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage
from app.events.repository import EventIngestResult, EventRepository
from app.events.service import EventService

ORIGIN_TIME = datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 9, 17, 2, 31, tzinfo=UTC)


def _event(
    *,
    kind: EventKind = EventKind.AUTO,
    source_event_id: str | None = "CENC-AUTO-1",
    magnitude: Decimal = Decimal("5.1"),
    origin_time: datetime = ORIGIN_TIME,
) -> NormalizedEvent:
    return NormalizedEvent(
        kind=kind,
        source="cenc",
        source_event_id=source_event_id,
        origin_time=origin_time,
        longitude=Decimal("121.540000"),
        latitude=Decimal("31.220000"),
        depth_km=Decimal("12.00"),
        magnitude=magnitude,
        place="Shanghai Pudong",
    )


def _raw_message(*, received_at: datetime = RECEIVED_AT) -> RawMessage:
    return RawMessage(
        id=uuid.uuid4(),
        source="cenc",
        source_message_id="CENC-AUTO-1",
        message_kind=EventKind.AUTO.value,
        received_at=received_at,
        checksum="a" * 64,
        payload={"type": "automatic"},
    )


def _canonical_event(
    *,
    event: NormalizedEvent,
    current_revision_id: uuid.UUID | None,
    current_kind: EventKind,
    canonical_source_id: str = "cenc:CENC-AUTO-1",
) -> EarthquakeEvent:
    return EarthquakeEvent(
        id=uuid.uuid4(),
        source=event.source,
        canonical_source_id=canonical_source_id,
        event_type=current_kind.value,
        origin_time=event.origin_time,
        longitude=event.longitude,
        latitude=event.latitude,
        depth_km=event.depth_km,
        magnitude=event.magnitude,
        place=event.place,
        geom=WKTElement(
            f"POINT({event.longitude} {event.latitude})",
            srid=4326,
        ),
        current_revision_id=current_revision_id,
        created_at=RECEIVED_AT - timedelta(minutes=1),
        updated_at=RECEIVED_AT - timedelta(minutes=1),
    )


class _Result:
    rowcount = 1


class _RecordingSession:
    def __init__(self, *scalar_results: object) -> None:
        self._scalar_results = list(scalar_results)
        self.scalar_statements: list[Any] = []
        self.added: list[object] = []
        self.flushed: list[object] = []
        self.executed: list[Any] = []

    async def scalar(self, statement: Any) -> object:
        self.scalar_statements.append(statement)
        if not self._scalar_results:
            raise AssertionError("unexpected scalar query")
        return self._scalar_results.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()
            self.flushed.append(value)

    async def execute(self, statement: Any) -> _Result:
        self.executed.append(statement)
        return _Result()


class _BeginContext:
    def __init__(self, session: "_ServiceSession") -> None:
        self._session = session

    async def __aenter__(self) -> "_ServiceSession":
        self._session.events.append("begin")
        return self._session

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._session.events.append("rollback" if exc_type else "commit")


class _ServiceSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> "_ServiceSession":
        self.events.append("session:enter")
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.events.append("session:exit")

    def begin(self) -> _BeginContext:
        return _BeginContext(self)


class _ServiceSessionFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.session = _ServiceSession(events)

    def __call__(self) -> _ServiceSession:
        self.events.append("session:create")
        return self.session


class _RecordingRepository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.raw_payload: dict[str, object] | None = None
        self.event: NormalizedEvent | None = None
        self.received_at: datetime | None = None
        self.session: _ServiceSession | None = None

    async def get_or_create_raw_message(
        self,
        session: _ServiceSession,
        raw_payload: dict[str, object],
        event: NormalizedEvent,
        received_at: datetime,
    ) -> RawMessage:
        self.events.append("get_or_create_raw_message")
        self.session = session
        self.raw_payload = raw_payload
        self.event = event
        self.received_at = received_at
        return RawMessage(
            id=uuid.uuid4(),
            source=event.source,
            source_message_id=event.source_event_id,
            message_kind=event.kind.value,
            received_at=received_at,
            checksum="b" * 64,
            payload=raw_payload,
        )

    async def append_revision(
        self,
        session: _ServiceSession,
        raw: RawMessage,
        event: NormalizedEvent,
    ) -> EventIngestResult:
        self.events.append("append_revision")
        assert session is self.session
        assert raw.payload == self.raw_payload
        assert event is self.event
        return EventIngestResult(
            event_id=str(uuid.uuid4()),
            revision_id=str(uuid.uuid4()),
            revision_no=1,
            event_kind=event.kind,
        )


def _compiled(statement: Any) -> tuple[str, dict[str, object]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def test_checksum_is_canonical_and_order_independent() -> None:
    repository = EventRepository()
    first = {"type": "reviewed", "data": {"b": 2, "a": 1}}
    reordered = {"data": {"a": 1, "b": 2}, "type": "reviewed"}

    first_checksum = repository.checksum(first, "cenc", EventKind.FORMAL.value)

    assert first_checksum == repository.checksum(
        reordered,
        "cenc",
        EventKind.FORMAL.value,
    )
    assert len(first_checksum) == 64
    assert first_checksum != repository.checksum(first, "other", EventKind.FORMAL.value)
    assert first_checksum != repository.checksum(first, "cenc", EventKind.AUTO.value)
    assert first_checksum != repository.checksum(
        {"type": "reviewed", "data": {"b": 3, "a": 1}},
        "cenc",
        EventKind.FORMAL.value,
    )


@pytest.mark.parametrize(
    ("payload", "error", "message"),
    [
        ({}, ValueError, "empty"),
        ({"value": float("nan")}, ValueError, "finite"),
        ({"value": object()}, TypeError, "JSON"),
        ({1: "non-string-key"}, TypeError, "JSON"),
    ],
)
def test_checksum_rejects_non_canonical_json(
    payload: dict[object, object],
    error: type[Exception],
    message: str,
) -> None:
    repository = EventRepository()

    with pytest.raises(error, match=message):
        repository.checksum(payload, "cenc", EventKind.AUTO.value)


async def test_get_or_create_raw_message_inserts_canonical_payload_and_flushes() -> None:
    repository = EventRepository()
    session = _RecordingSession(None)
    event = _event()
    payload = {"type": "automatic", "EventID": "CENC-AUTO-1"}

    raw = await repository.get_or_create_raw_message(
        session,
        payload,
        event,
        RECEIVED_AT,
    )

    assert raw in session.added
    assert raw in session.flushed
    assert raw.id is not None
    assert raw.source == "cenc"
    assert raw.source_message_id == "CENC-AUTO-1"
    assert raw.message_kind == EventKind.AUTO.value
    assert raw.payload == payload
    assert raw.checksum == repository.checksum(
        payload,
        event.source,
        event.kind.value,
    )
    sql, _ = _compiled(session.scalar_statements[0])
    assert "raw_messages.checksum" in sql


async def test_get_or_create_raw_message_returns_existing_without_insert() -> None:
    repository = EventRepository()
    existing = _raw_message()
    session = _RecordingSession(existing)

    raw = await repository.get_or_create_raw_message(
        session,
        existing.payload,
        _event(),
        RECEIVED_AT,
    )

    assert raw is existing
    assert session.added == []
    assert session.flushed == []


async def test_new_event_and_first_revision_use_structured_point_geometry() -> None:
    repository = EventRepository()
    session = _RecordingSession(None, None, None)
    raw = _raw_message()
    event = _event()

    result = await repository.append_revision(session, raw, event)

    canonical = next(value for value in session.added if isinstance(value, EarthquakeEvent))
    revision = next(value for value in session.added if isinstance(value, EarthquakeRevision))
    assert isinstance(canonical.geom, WKTElement)
    assert canonical.geom.srid == 4326
    assert canonical.geom.data == "POINT(121.540000 31.220000)"
    assert canonical.current_revision_id == revision.id
    assert revision.is_current is True
    assert result.revision_no == 1
    assert result.event_kind is EventKind.AUTO


async def test_exact_source_lookup_locks_event_and_appends_current_formal_revision() -> None:
    repository = EventRepository()
    old_revision_id = uuid.uuid4()
    initial = _event()
    existing = _canonical_event(
        event=initial,
        current_revision_id=old_revision_id,
        current_kind=EventKind.AUTO,
    )
    session = _RecordingSession(None, existing, 1)
    raw = _raw_message()
    formal = _event(kind=EventKind.FORMAL, magnitude=Decimal("5.2"))

    result = await repository.append_revision(session, raw, formal)

    assert result.revision_no == 2
    assert result.event_kind is EventKind.FORMAL
    assert existing.magnitude == Decimal("5.2")
    assert existing.event_type == EventKind.FORMAL.value
    assert existing.current_revision_id is not None
    revision = next(value for value in session.added if isinstance(value, EarthquakeRevision))
    assert revision.is_current is True
    assert len(session.executed) == 1
    sql, _ = _compiled(session.scalar_statements[1])
    assert "earthquake_events.canonical_source_id" in sql
    assert "FOR UPDATE" in sql
    update_sql, _ = _compiled(session.executed[0])
    assert "UPDATE earthquake_revisions" in update_sql
    assert "SET is_current=" in update_sql


async def test_tolerance_lookup_uses_source_time_window_spatial_distance_and_lock() -> None:
    repository = EventRepository()
    initial = _event()
    existing = _canonical_event(
        event=initial,
        current_revision_id=uuid.uuid4(),
        current_kind=EventKind.AUTO,
    )
    session = _RecordingSession(None, None, existing, 1)
    formal = _event(
        kind=EventKind.FORMAL,
        source_event_id="CENC-FORMAL-2",
        magnitude=Decimal("5.2"),
    )

    await repository.append_revision(session, _raw_message(), formal)

    sql, params = _compiled(session.scalar_statements[2])
    assert "earthquake_events.source" in sql
    assert "epoch" in sql
    assert "ST_DWithin" in sql
    assert "FOR UPDATE" in sql
    assert 120 in params.values()
    assert 0.2 in params.values()
    assert existing.canonical_source_id == "cenc:CENC-FORMAL-2"


async def test_late_auto_after_formal_merges_without_replacing_published_version() -> None:
    repository = EventRepository()
    current_revision_id = uuid.uuid4()
    formal = _event(
        kind=EventKind.FORMAL,
        source_event_id="CENC-FORMAL-2",
        magnitude=Decimal("5.2"),
        origin_time=ORIGIN_TIME + timedelta(seconds=30),
    )
    existing = _canonical_event(
        event=formal,
        current_revision_id=current_revision_id,
        current_kind=EventKind.FORMAL,
        canonical_source_id="cenc:CENC-FORMAL-2",
    )
    session = _RecordingSession(None, None, existing, 1)
    late_auto = _event(
        source_event_id="CENC-AUTO-1",
        magnitude=Decimal("5.0"),
        origin_time=ORIGIN_TIME + timedelta(seconds=20),
    )

    result = await repository.append_revision(
        session,
        _raw_message(received_at=RECEIVED_AT + timedelta(minutes=2)),
        late_auto,
    )

    revision = next(value for value in session.added if isinstance(value, EarthquakeRevision))
    assert revision.is_current is False
    assert result.revision_no == 2
    assert existing.current_revision_id == current_revision_id
    assert existing.event_type == EventKind.FORMAL.value
    assert existing.magnitude == Decimal("5.2")
    assert existing.canonical_source_id == "cenc:CENC-FORMAL-2"
    assert session.executed == []


async def test_duplicate_raw_message_returns_existing_revision_idempotently() -> None:
    repository = EventRepository()
    raw = _raw_message()
    revision_id = uuid.uuid4()
    existing_revision = EarthquakeRevision(
        id=revision_id,
        event_id=uuid.uuid4(),
        raw_message_id=raw.id,
        revision_no=3,
        revision_kind=EventKind.CORRECTION.value,
        source_event_id="CENC-FORMAL-2",
        origin_time=ORIGIN_TIME,
        longitude=Decimal("121.540000"),
        latitude=Decimal("31.220000"),
        depth_km=Decimal("12.00"),
        magnitude=Decimal("5.2"),
        place="Shanghai Pudong",
        is_current=True,
        created_at=RECEIVED_AT,
    )
    session = _RecordingSession(existing_revision)

    result = await repository.append_revision(session, raw, _event(kind=EventKind.CORRECTION))

    assert result == EventIngestResult(
        event_id=str(existing_revision.event_id),
        revision_id=str(revision_id),
        revision_no=3,
        event_kind=EventKind.CORRECTION,
    )
    assert len(session.scalar_statements) == 1
    assert session.added == []
    assert session.executed == []


async def test_service_wraps_raw_event_and_revision_work_in_one_transaction() -> None:
    events: list[str] = []
    session_factory = _ServiceSessionFactory(events)
    repository = _RecordingRepository(events)
    service = EventService(session_factory, repository=repository)
    payload = {"type": "automatic", "EventID": "CENC-AUTO-1"}
    event = _event()
    china_time = timezone(timedelta(hours=8))
    received_at = datetime(2026, 9, 17, 10, 31, tzinfo=china_time)

    result = await service.ingest(payload, event, received_at=received_at)

    assert events == [
        "session:create",
        "session:enter",
        "begin",
        "get_or_create_raw_message",
        "append_revision",
        "commit",
        "session:exit",
    ]
    assert repository.raw_payload == payload
    assert repository.event is event
    assert repository.received_at == RECEIVED_AT
    assert repository.session is session_factory.session
    assert result.event_kind is EventKind.AUTO


@pytest.mark.parametrize(
    ("payload", "received_at", "error", "message"),
    [
        ({}, RECEIVED_AT, ValueError, "empty"),
        ({"value": object()}, RECEIVED_AT, TypeError, "JSON"),
        (
            {"type": "automatic"},
            datetime(2026, 9, 17, 10, 31),
            ValueError,
            "timezone",
        ),
    ],
)
async def test_service_rejects_invalid_input_before_opening_a_transaction(
    payload: dict[object, object],
    received_at: datetime,
    error: type[Exception],
    message: str,
) -> None:
    events: list[str] = []
    session_factory = _ServiceSessionFactory(events)
    repository = _RecordingRepository(events)
    service = EventService(session_factory, repository=repository)

    with pytest.raises(error, match=message):
        await service.ingest(payload, _event(), received_at=received_at)

    assert events == []
