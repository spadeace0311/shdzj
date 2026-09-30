import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy.dialects import postgresql

from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage
from app.events.repository import (
    EventIngestResult,
    EventRepository,
    _becomes_current,
)
from app.events.response_rules import ResponseInput, ResponseSuggestion
from app.events.service import EventService

ORIGIN_TIME = datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 9, 17, 2, 31, tzinfo=UTC)


def _event(kind: EventKind = EventKind.FORMAL) -> NormalizedEvent:
    return NormalizedEvent(
        kind=kind,
        source="cenc",
        source_event_id="CENC-2026-0001",
        origin_time=ORIGIN_TIME,
        longitude=Decimal("121.540000"),
        latitude=Decimal("31.220000"),
        depth_km=Decimal("12.00"),
        magnitude=Decimal("5.2"),
        place="Shanghai Pudong",
        report_time=RECEIVED_AT,
        report_number=1,
    )


def _revision(
    *,
    event_id: uuid.UUID,
    kind: EventKind,
    is_current: bool,
    created_at: datetime = RECEIVED_AT,
) -> EarthquakeRevision:
    return EarthquakeRevision(
        id=uuid.uuid4(),
        event_id=event_id,
        raw_message_id=uuid.uuid4(),
        revision_no=1,
        revision_kind=kind.value,
        source_event_id="CENC-2026-0001",
        source_report_time=RECEIVED_AT,
        source_report_number=1,
        origin_time=ORIGIN_TIME,
        longitude=Decimal("121.540000"),
        latitude=Decimal("31.220000"),
        depth_km=Decimal("12.00"),
        magnitude=Decimal("5.2"),
        place="Shanghai Pudong",
        is_current=is_current,
        created_at=created_at,
    )


def _canonical_event(*, event_id: uuid.UUID) -> EarthquakeEvent:
    return EarthquakeEvent(
        id=event_id,
        source="cenc",
        canonical_source_id="cenc:CENC-2026-0001",
        event_type=EventKind.FORMAL.value,
        origin_time=ORIGIN_TIME,
        longitude=Decimal("121.540000"),
        latitude=Decimal("31.220000"),
        depth_km=Decimal("12.00"),
        magnitude=Decimal("5.2"),
        place="Shanghai Pudong",
        geom=WKTElement("POINT(121.540000 31.220000)", srid=4326),
        institutional_level="larger",
        service_level=3,
        response_suggestion={"institutional_level": "larger"},
        response_rule_version="2026.0",
        created_at=RECEIVED_AT - timedelta(minutes=1),
        updated_at=RECEIVED_AT - timedelta(minutes=1),
    )


def _suggestion(level: str = "major") -> ResponseSuggestion:
    return ResponseSuggestion(
        institutional_level=level,
        service_level=2,
        downgraded=False,
        causes=("test",),
        rule_version="2026.1",
    )


def _payload() -> dict[str, object]:
    return {"reportType": "formal", "originTime": ORIGIN_TIME.isoformat()}


class _SuggestionSession:
    def __init__(self, objects: dict[tuple[type, uuid.UUID], object]) -> None:
        self._objects = objects
        self.gets: list[tuple[type, uuid.UUID, bool]] = []

    async def get(
        self,
        model: type,
        ident: uuid.UUID,
        with_for_update: bool = False,
    ) -> object | None:
        self.gets.append((model, ident, with_for_update))
        return self._objects.get((model, ident))


class _AtomicBegin:
    def __init__(self, session: "_AtomicSession") -> None:
        self._session = session

    async def __aenter__(self) -> "_AtomicSession":
        self._session.events.append("begin")
        return self._session

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._session.events.append("rollback" if exc_type else "commit")


class _AtomicSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> "_AtomicSession":
        self.events.append("session:enter")
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.events.append("session:exit")

    def begin(self) -> _AtomicBegin:
        return _AtomicBegin(self)


class _AtomicSessionFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.session = _AtomicSession(events)

    def __call__(self) -> _AtomicSession:
        self.events.append("session:create")
        return self.session


class _AtomicRegionRepository:
    async def get_active(self, session: object):
        del session
        return type(
            "_ActiveBoundary",
            (),
            {"version": "atomic-boundary-v1"},
        )()


class _AtomicRepository:
    def __init__(
        self,
        events: list[str],
        *,
        suggestion_error: bool = False,
        current_levels: tuple[str | None, int | None] = ("major", 2),
        append_is_current: bool = True,
        enqueue_result: bool = True,
    ) -> None:
        self.events = events
        self.suggestion_error = suggestion_error
        self.current_levels = current_levels
        self.append_is_current = append_is_current
        self.enqueue_result = enqueue_result
        self.suggestion_calls = 0

    async def acquire_ingest_lock(self, session: object, source: str) -> None:
        del session, source
        self.events.append("acquire_ingest_lock")

    async def get_or_create_raw_message(
        self,
        session: object,
        payload: dict[str, object],
        event: NormalizedEvent,
        received_at: datetime,
        *,
        provider: str,
        ingest_lane: str,
    ) -> RawMessage:
        del session, received_at
        self.events.append("get_or_create_raw_message")
        return RawMessage(
            id=uuid.uuid4(),
            source=event.source,
            source_message_id=event.source_event_id,
            message_kind=event.kind.value,
            received_at=RECEIVED_AT,
            checksum="a" * 64,
            payload=payload,
        )

    async def append_revision(
        self,
        session: object,
        raw: RawMessage,
        event: NormalizedEvent,
        *,
        semantic_fingerprint: str | None,
        provider: str,
        ingest_lane: str,
        ingested_at: datetime,
        region_context: object | None,
    ) -> EventIngestResult:
        del session, raw
        self.events.append("append_revision")
        return EventIngestResult(
            event_id="event-1",
            revision_id="revision-1",
            revision_no=1,
            event_kind=event.kind,
            is_current=self.append_is_current,
        )

    async def enqueue_assessment(
        self,
        session: object,
        *,
        event_id: object,
        revision_id: object,
        revision_no: int,
        trigger_reason: str,
        created_at: datetime,
    ) -> bool:
        del session, event_id, revision_id, revision_no, trigger_reason, created_at
        self.events.append("enqueue_assessment")
        return self.enqueue_result

    async def set_revision_suggestion(
        self,
        session: object,
        revision_id: str,
        suggestion: ResponseSuggestion,
        suggestion_payload: dict,
    ) -> None:
        del session, revision_id, suggestion, suggestion_payload
        self.events.append("set_revision_suggestion")
        self.suggestion_calls += 1
        if self.suggestion_error:
            raise RuntimeError("suggestion failure")

    async def get_current_response_levels(
        self,
        session: object,
        event_id: str,
    ) -> tuple[str | None, int | None]:
        del session, event_id
        self.events.append("get_current_response_levels")
        return self.current_levels

    async def get_event_lifecycle_snapshot(
        self,
        session: object,
        event_id: str,
    ) -> tuple[str | None, datetime | None]:
        del session, event_id
        self.events.append("get_event_lifecycle_snapshot")
        return "formal_triggered", RECEIVED_AT


def _compiled(statement: object) -> tuple[str, dict[str, object]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


async def test_repository_saves_revision_snapshot_without_updating_non_current_event() -> None:
    event = _canonical_event(event_id=uuid.uuid4())
    revision = _revision(event_id=event.id, kind=EventKind.CORRECTION, is_current=False)
    repository = EventRepository()
    session = _SuggestionSession(
        {
            (EarthquakeRevision, revision.id): revision,
            (EarthquakeEvent, event.id): event,
        }
    )
    suggestion = _suggestion("major")
    payload = {"institutional_level": "major", "causes": ["test"]}

    await repository.set_revision_suggestion(
        session,
        str(revision.id),
        suggestion,
        payload,
    )

    assert revision.institutional_level == "major"
    assert revision.response_suggestion == payload
    assert event.institutional_level == "larger"
    assert event.response_suggestion == {"institutional_level": "larger"}


async def test_repository_updates_current_event_redundancy_for_current_revision() -> None:
    event = _canonical_event(event_id=uuid.uuid4())
    revision = _revision(event_id=event.id, kind=EventKind.CORRECTION, is_current=True)
    repository = EventRepository()
    session = _SuggestionSession(
        {
            (EarthquakeRevision, revision.id): revision,
            (EarthquakeEvent, event.id): event,
        }
    )
    suggestion = _suggestion("major")
    payload = {"institutional_level": "major", "causes": ["test"]}

    await repository.set_revision_suggestion(
        session,
        str(revision.id),
        suggestion,
        payload,
    )

    assert revision.institutional_level == "major"
    assert event.institutional_level == "major"
    assert event.response_rule_version == "2026.1"


async def test_repository_keeps_previous_revision_snapshot_after_recalculation() -> None:
    event = _canonical_event(event_id=uuid.uuid4())
    old_revision = _revision(event_id=event.id, kind=EventKind.FORMAL, is_current=False)
    new_revision = _revision(event_id=event.id, kind=EventKind.CORRECTION, is_current=True)
    repository = EventRepository()
    session = _SuggestionSession(
        {
            (EarthquakeRevision, old_revision.id): old_revision,
            (EarthquakeRevision, new_revision.id): new_revision,
            (EarthquakeEvent, event.id): event,
        }
    )

    await repository.set_revision_suggestion(
        session,
        str(old_revision.id),
        _suggestion("larger"),
        {"institutional_level": "larger"},
    )
    await repository.set_revision_suggestion(
        session,
        str(new_revision.id),
        _suggestion("major"),
        {"institutional_level": "major"},
    )

    assert old_revision.institutional_level == "larger"
    assert old_revision.response_suggestion == {"institutional_level": "larger"}
    assert new_revision.institutional_level == "major"


async def test_service_commits_revision_and_suggestion_in_one_transaction() -> None:
    events: list[str] = []
    repository = _AtomicRepository(events)
    service = EventService(
        _AtomicSessionFactory(events),
        repository=repository,
        region_repository=_AtomicRegionRepository(),
    )

    outcome = await service.ingest_with_response_suggestion(
        _payload(),
        _event(EventKind.FORMAL),
        ResponseInput(
            magnitude=Decimal("5.2"),
            depth_km=Decimal("12"),
            inside_shanghai=True,
            distance_to_boundary_km=None,
            deaths=None,
            max_intensity=Decimal("6"),
        ),
    )

    assert events == [
        "session:create",
        "session:enter",
        "begin",
        "acquire_ingest_lock",
        "get_or_create_raw_message",
        "append_revision",
        "set_revision_suggestion",
        "enqueue_assessment",
        "get_current_response_levels",
        "get_event_lifecycle_snapshot",
        "commit",
        "session:exit",
    ]
    assert outcome.institutional_level == "major"
    assert outcome.service_level == 2


async def test_service_rolls_back_revision_when_suggestion_persist_fails() -> None:
    events: list[str] = []
    repository = _AtomicRepository(events, suggestion_error=True)
    service = EventService(
        _AtomicSessionFactory(events),
        repository=repository,
        region_repository=_AtomicRegionRepository(),
    )

    with pytest.raises(RuntimeError, match="suggestion failure"):
        await service.ingest_with_response_suggestion(
            _payload(),
            _event(EventKind.FORMAL),
            ResponseInput(
                magnitude=Decimal("5.2"),
                depth_km=Decimal("12"),
                inside_shanghai=True,
                distance_to_boundary_km=None,
                deaths=None,
                max_intensity=Decimal("6"),
            ),
        )

    assert "append_revision" in events
    assert "set_revision_suggestion" in events
    assert "get_current_response_levels" not in events
    assert events[-2:] == ["rollback", "session:exit"]
    assert "commit" not in events


async def test_service_without_context_returns_existing_current_suggestion() -> None:
    events: list[str] = []
    repository = _AtomicRepository(events, current_levels=("larger", 3))
    service = EventService(
        _AtomicSessionFactory(events),
        repository=repository,
        region_repository=_AtomicRegionRepository(),
    )

    outcome = await service.ingest_with_response_suggestion(
        _payload(),
        _event(EventKind.FORMAL),
        None,
    )

    assert repository.suggestion_calls == 0
    assert outcome.institutional_level == "larger"
    assert outcome.service_level == 3


async def test_triggered_assessment_reflects_enqueue_noop_result() -> None:
    events: list[str] = []
    repository = _AtomicRepository(events, enqueue_result=False)
    service = EventService(
        _AtomicSessionFactory(events),
        repository=repository,
        region_repository=_AtomicRegionRepository(),
    )

    outcome = await service.ingest_with_response_suggestion(
        _payload(),
        _event(EventKind.FORMAL),
        ResponseInput(
            magnitude=Decimal("5.2"),
            depth_km=Decimal("12"),
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            deaths=None,
            max_intensity=Decimal("6"),
        ),
    )

    assert "enqueue_assessment" in events
    assert outcome.triggered_assessment is False


async def test_non_current_formal_with_context_keeps_current_suggestion() -> None:
    events: list[str] = []
    repository = _AtomicRepository(
        events,
        current_levels=("larger", 3),
        append_is_current=False,
    )
    service = EventService(
        _AtomicSessionFactory(events),
        repository=repository,
        region_repository=_AtomicRegionRepository(),
    )

    outcome = await service.ingest_with_response_suggestion(
        _payload(),
        _event(EventKind.FORMAL),
        ResponseInput(
            magnitude=Decimal("5.2"),
            depth_km=Decimal("12"),
            inside_shanghai=True,
            distance_to_boundary_km=None,
            deaths=None,
            max_intensity=Decimal("6"),
        ),
    )

    assert "set_revision_suggestion" not in events
    assert "enqueue_assessment" not in events
    assert outcome.institutional_level == "larger"
    assert outcome.service_level == 3


def test_late_test_or_drill_does_not_replace_real_current_revision() -> None:
    event_id = uuid.uuid4()
    real_revision = _revision(
        event_id=event_id,
        kind=EventKind.MANUAL,
        is_current=True,
        created_at=RECEIVED_AT,
    )
    late = RECEIVED_AT + timedelta(hours=1)

    assert _becomes_current(real_revision, _event(EventKind.TEST), late) is False
    assert _becomes_current(real_revision, _event(EventKind.DRILL), late) is False


def test_manual_replaces_test_or_drill_current_revision() -> None:
    event_id = uuid.uuid4()
    test_revision = _revision(
        event_id=event_id,
        kind=EventKind.TEST,
        is_current=True,
        created_at=RECEIVED_AT,
    )

    assert (
        _becomes_current(
            test_revision,
            _event(EventKind.MANUAL),
            RECEIVED_AT + timedelta(seconds=1),
        )
        is True
    )


def test_late_test_or_drill_does_not_replace_current_auto() -> None:
    event_id = uuid.uuid4()
    auto_revision = _revision(
        event_id=event_id,
        kind=EventKind.AUTO,
        is_current=True,
        created_at=RECEIVED_AT,
    )
    late = RECEIVED_AT + timedelta(hours=1)

    assert _becomes_current(auto_revision, _event(EventKind.TEST), late) is False
    assert _becomes_current(auto_revision, _event(EventKind.DRILL), late) is False


def test_auto_replaces_test_or_drill_current_revision() -> None:
    event_id = uuid.uuid4()
    test_revision = _revision(
        event_id=event_id,
        kind=EventKind.TEST,
        is_current=True,
        created_at=RECEIVED_AT,
    )

    assert (
        _becomes_current(
            test_revision,
            _event(EventKind.AUTO),
            RECEIVED_AT + timedelta(seconds=1),
        )
        is True
    )


async def test_new_current_revision_without_snapshot_clears_event_redundancy() -> None:
    event = _canonical_event(event_id=uuid.uuid4())
    event.institutional_level = "major"
    event.service_level = 2
    event.response_suggestion = {"institutional_level": "major"}
    event.response_rule_version = "2026.1"

    current_revision = _revision(
        event_id=event.id,
        kind=EventKind.FORMAL,
        is_current=True,
    )
    current_revision.institutional_level = "major"
    current_revision.service_level = 2
    current_revision.response_suggestion = {"institutional_level": "major"}
    current_revision.response_rule_version = "2026.1"
    event.current_revision_id = current_revision.id

    raw = RawMessage(
        id=uuid.uuid4(),
        source="cenc",
        source_message_id="CENC-2026-0001",
        message_kind=EventKind.CORRECTION.value,
        received_at=RECEIVED_AT + timedelta(minutes=5),
        checksum="b" * 64,
        payload={"reportType": "correction"},
    )
    session = _AppendSession(None, event, 1, current_revision, ["formal"], None)
    incoming = _event(EventKind.CORRECTION)

    result = await EventRepository().append_revision(
        session,
        raw,
        incoming,
        semantic_fingerprint="f" * 64,
        provider="api",
        ingest_lane="http",
        ingested_at=raw.received_at,
        region_context=None,
    )

    assert result.is_current is True
    assert event.institutional_level is None
    assert event.service_level is None
    assert event.response_suggestion is None
    assert event.response_rule_version is None
    assert current_revision.institutional_level == "major"
    assert current_revision.response_suggestion == {"institutional_level": "major"}


async def test_list_query_orders_real_events_before_test_and_drill() -> None:
    session = _ListSession()

    await EventRepository().list_current_events(session)

    sql, params = _compiled(session.statement)
    assert "CASE" in sql
    assert "ORDER BY" in sql
    event_types = next(value for value in params.values() if isinstance(value, list))
    assert "auto" in event_types


class _AppendResult:
    rowcount = 1


class _AppendSession:
    def __init__(self, *scalar_results: object) -> None:
        self._scalar_results = list(scalar_results)
        self.scalar_statements: list[object] = []
        self.added: list[object] = []
        self.flushed: list[object] = []
        self.executed: list[tuple[object, object]] = []

    async def scalar(self, statement: object) -> object:
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

    async def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> _AppendResult:
        self.executed.append((statement, parameters))
        return _AppendResult()


class _ListResult:
    def all(self) -> list[tuple[object, object]]:
        return []


class _ListSession:
    def __init__(self) -> None:
        self.statement: object | None = None

    async def execute(self, statement: object) -> _ListResult:
        self.statement = statement
        return _ListResult()
