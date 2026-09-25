from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.events.domain import NormalizedEvent
from app.events.repository import (
    EventDetailRecord,
    EventIngestResult,
    EventRepository,
    EventSummaryRecord,
)
from app.events.response_rules import (
    ResponseInput,
    ResponseRuleEngine,
    ResponseSuggestion,
)
from app.events.schemas import EventDetailResponse, EventSummaryResponse


class EventService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository: EventRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or EventRepository(session_factory)

    async def ingest(
        self,
        raw_payload: dict[str, object],
        event: NormalizedEvent,
        received_at: datetime | None = None,
    ) -> EventIngestResult:
        if not isinstance(event, NormalizedEvent):
            raise TypeError("event must be a NormalizedEvent")
        EventRepository.validate_payload(raw_payload)
        normalized_received_at = _normalize_received_at(received_at)

        async with self._session_factory() as session:
            async with session.begin():
                await self._repository.acquire_ingest_lock(session, event.source)
                raw = await self._repository.get_or_create_raw_message(
                    session,
                    raw_payload,
                    event,
                    normalized_received_at,
                )
                return await self._repository.append_revision(session, raw, event)

    async def apply_response_suggestion(
        self,
        event_id: str,
        value: ResponseInput,
    ) -> ResponseSuggestion:
        engine = ResponseRuleEngine.from_yaml(settings.response_rules_path)
        suggestion = engine.suggest(value)
        async with self._session_factory() as session:
            async with session.begin():
                suggestion_payload = asdict(suggestion)
                suggestion_payload["causes"] = list(suggestion.causes)
                await self._repository.set_response_suggestion(
                    session,
                    event_id,
                    suggestion,
                    suggestion_payload,
                )
        return suggestion

    async def list_events(self) -> list[EventSummaryResponse]:
        async with self._session_factory() as session:
            async with session.begin():
                records = await self._repository.list_current_events(session)
        return [_summary_response(record) for record in records]

    async def get_event(self, event_id: str) -> EventDetailResponse:
        async with self._session_factory() as session:
            async with session.begin():
                record = await self._repository.get_current_event(session, event_id)
        return _detail_response(record)


def _summary_response(record: EventSummaryRecord) -> EventSummaryResponse:
    return EventSummaryResponse(
        id=record.event_id,
        source=record.source,
        event_kind=record.event_kind,
        place=record.place,
        magnitude=record.magnitude,
        depth_km=record.depth_km,
        origin_time=record.origin_time,
        longitude=record.longitude,
        latitude=record.latitude,
        institutional_level=record.institutional_level,
        service_level=record.service_level,
        revision_no=record.revision_no,
    )


def _detail_response(record: EventDetailRecord) -> EventDetailResponse:
    return EventDetailResponse(
        id=record.event_id,
        source=record.source,
        place=record.place,
        magnitude=record.magnitude,
        depth_km=record.depth_km,
        origin_time=record.origin_time,
        longitude=record.longitude,
        latitude=record.latitude,
        institutional_level=record.institutional_level,
        service_level=record.service_level,
        response_suggestion=record.response_suggestion,
        response_rule_version=record.response_rule_version,
        revision_no=record.revision_no,
    )


def _normalize_received_at(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, datetime):
        raise TypeError("received_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("received_at must include timezone information")
    return value.astimezone(UTC)
