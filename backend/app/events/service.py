from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.domain import NormalizedEvent
from app.events.repository import EventIngestResult, EventRepository


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


def _normalize_received_at(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, datetime):
        raise TypeError("received_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("received_at must include timezone information")
    return value.astimezone(UTC)
