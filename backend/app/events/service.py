from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.events.domain import NormalizedEvent
from app.events.repository import (
    EventDetailRecord,
    EventIngestOutcome,
    EventIngestResult,
    EventRepository,
    EventSummaryRecord,
)
from app.events.response_rules import (
    ResponseInput,
    ResponseRuleEngine,
)


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

    async def ingest_with_response_suggestion(
        self,
        raw_payload: dict[str, object],
        event: NormalizedEvent,
        response_input: ResponseInput | None = None,
        received_at: datetime | None = None,
    ) -> EventIngestOutcome:
        if not isinstance(event, NormalizedEvent):
            raise TypeError("event must be a NormalizedEvent")
        EventRepository.validate_payload(raw_payload)
        normalized_received_at = _normalize_received_at(received_at)
        suggestion = None
        if response_input is not None:
            engine = ResponseRuleEngine.from_yaml(settings.response_rules_path)
            suggestion = engine.suggest(response_input)
        suggestion_payload = None
        if suggestion is not None:
            suggestion_payload = asdict(suggestion)
            suggestion_payload["causes"] = list(suggestion.causes)

        async with self._session_factory() as session:
            async with session.begin():
                await self._repository.acquire_ingest_lock(session, event.source)
                raw = await self._repository.get_or_create_raw_message(
                    session,
                    raw_payload,
                    event,
                    normalized_received_at,
                )
                result = await self._repository.append_revision(session, raw, event)
                if suggestion is not None:
                    await self._repository.set_revision_suggestion(
                        session,
                        result.revision_id,
                        suggestion,
                        suggestion_payload,
                    )
                (
                    institutional_level,
                    service_level,
                ) = await self._repository.get_current_response_levels(
                    session,
                    result.event_id,
                )
        return EventIngestOutcome(
            event_id=result.event_id,
            revision_id=result.revision_id,
            revision_no=result.revision_no,
            event_kind=result.event_kind,
            is_current=result.is_current,
            institutional_level=institutional_level,
            service_level=service_level,
        )

    async def list_events(self) -> list[EventSummaryRecord]:
        async with self._session_factory() as session:
            async with session.begin():
                return await self._repository.list_current_events(session)

    async def get_event(
        self,
        event_id: str,
    ) -> EventDetailRecord:
        async with self._session_factory() as session:
            async with session.begin():
                return await self._repository.get_current_event(session, event_id)


def _normalize_received_at(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, datetime):
        raise TypeError("received_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("received_at must include timezone information")
    return value.astimezone(UTC)
