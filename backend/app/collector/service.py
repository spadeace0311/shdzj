from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collector.coordinator import CancellationIgnored
from app.collector.domain import ProviderHealthUpdate
from app.collector.models import CollectorDeadLetter, CollectorRuntimeState
from app.events.models import EarthquakeRevision

_ALLOWED_DEAD_LETTER_STATUSES = {"open", "retried", "resolved"}
_MAX_ERROR_MESSAGE_LENGTH = 2_000


def classify_collector_error(exc: Exception) -> str:
    if isinstance(exc, (TypeError, ValueError, CancellationIgnored)):
        return "parse_error"
    if isinstance(exc, SQLAlchemyError):
        return "storage_error"
    return "internal_error"


def _provider_value(provider: object) -> str:
    return getattr(provider, "value", provider)


def _safe_error_message(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:_MAX_ERROR_MESSAGE_LENGTH]


class CollectorService:
    """Database-backed persistence for collector health, dead letters, and watermarks."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def persist_health(self, update: ProviderHealthUpdate) -> None:
        now = datetime.now(UTC)
        values = {
            "provider": update.provider.value,
            "state": update.state,
            "connected": update.connected,
            "last_http_status": update.last_http_status,
            "last_connected_at": update.last_connected_at,
            "last_message_at": update.last_message_at,
            "last_success_at": update.last_success_at,
            "consecutive_failures": update.consecutive_failures,
            "reconnect_count": update.reconnect_count,
            "last_error": update.last_error,
            "updated_at": now,
        }
        statement = pg_insert(CollectorRuntimeState).values(**values)
        update_columns = {
            key: getattr(statement.excluded, key)
            for key in values
            if key != "provider"
        }
        statement = statement.on_conflict_do_update(
            index_elements=[CollectorRuntimeState.provider],
            set_=update_columns,
        )
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(statement)

    async def get_last_processed_source_time(
        self,
        provider: object,
    ) -> datetime | None:
        provider_value = _provider_value(provider)
        async with self._session_factory() as session:
            return await session.scalar(
                select(CollectorRuntimeState.last_processed_source_time).where(
                    CollectorRuntimeState.provider == provider_value
                )
            )

    async def update_after_success(
        self,
        provider: object,
        ingested_at: datetime,
        source_time: datetime | None,
    ) -> None:
        provider_value = _provider_value(provider)
        now = datetime.now(UTC)
        statement = pg_insert(CollectorRuntimeState).values(
            provider=provider_value,
            state="healthy",
            connected=False,
            last_success_at=ingested_at,
            last_processed_source_time=source_time,
            updated_at=now,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[CollectorRuntimeState.provider],
            set_={
                "last_success_at": statement.excluded.last_success_at,
                "last_processed_source_time": statement.excluded.last_processed_source_time,
                "updated_at": statement.excluded.updated_at,
            },
        )
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(statement)

    async def get_last_ingested_event_id(self) -> str | None:
        async with self._session_factory() as session:
            event_id = await session.scalar(
                select(EarthquakeRevision.event_id)
                .where(EarthquakeRevision.ingested_at.is_not(None))
                .order_by(
                    EarthquakeRevision.ingested_at.desc(),
                    EarthquakeRevision.revision_no.desc(),
                )
                .limit(1)
            )
            return str(event_id) if event_id is not None else None

    async def load_dead_letter(
        self,
        dead_letter_id: object,
    ) -> dict[str, object] | None:
        try:
            identifier = uuid.UUID(str(dead_letter_id))
        except (TypeError, ValueError) as exc:
            raise LookupError(f"dead letter not found: {dead_letter_id}") from exc

        async with self._session_factory() as session:
            row = await session.get(CollectorDeadLetter, identifier)
            if row is None:
                raise LookupError(f"dead letter not found: {dead_letter_id}")
            return {
                "raw_payload": row.raw_payload,
                "provider": row.provider,
                "lane": row.lane,
                "received_at": row.received_at,
                "status": row.status,
            }

    async def mark_dead_letter(
        self,
        dead_letter_id: object,
        status: str,
        error: Exception | None = None,
    ) -> None:
        if status not in _ALLOWED_DEAD_LETTER_STATUSES:
            raise ValueError("dead letter status must be open, retried, or resolved")
        try:
            identifier = uuid.UUID(str(dead_letter_id))
        except (TypeError, ValueError) as exc:
            raise LookupError(f"dead letter not found: {dead_letter_id}") from exc

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(CollectorDeadLetter, identifier)
                if row is None:
                    raise LookupError(f"dead letter not found: {dead_letter_id}")
                row.status = status
                if error is not None:
                    row.error_message = _safe_error_message(error)
                    row.last_failed_at = datetime.now(UTC)

    async def record_dead_letter(
        self,
        *,
        provider: object,
        lane: object,
        raw_payload: dict[str, object],
        received_at: datetime,
        error_category: str,
        error_message: str,
        source_message_id: str | None = None,
    ) -> None:
        provider_value = _provider_value(provider)
        lane_value = _provider_value(lane)
        now = datetime.now(UTC)

        async with self._session_factory() as session:
            async with session.begin():
                existing = None
                if source_message_id:
                    existing = await session.scalar(
                        select(CollectorDeadLetter)
                        .where(
                            CollectorDeadLetter.provider == provider_value,
                            CollectorDeadLetter.lane == lane_value,
                            CollectorDeadLetter.source_message_id == source_message_id,
                        )
                        .order_by(CollectorDeadLetter.last_failed_at.desc())
                        .limit(1)
                    )

                if existing is not None:
                    existing.error_category = error_category
                    existing.error_message = error_message[:_MAX_ERROR_MESSAGE_LENGTH]
                    existing.status = "open"
                    existing.last_failed_at = now
                    return

                session.add(
                    CollectorDeadLetter(
                        id=uuid.uuid4(),
                        provider=provider_value,
                        lane=lane_value,
                        source_message_id=source_message_id,
                        raw_payload=raw_payload,
                        received_at=received_at,
                        error_category=error_category,
                        error_message=error_message[:_MAX_ERROR_MESSAGE_LENGTH],
                        status="open",
                        first_failed_at=now,
                        last_failed_at=now,
                    )
                )
