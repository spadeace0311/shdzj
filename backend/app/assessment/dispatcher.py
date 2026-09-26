from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.models import EventLifecycleOutbox


class WorkflowStarter(Protocol):
    async def start_assessment(
        self,
        *,
        workflow_id: str,
        payload: dict[str, object],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _ClaimedOutbox:
    id: object
    event_id: object
    revision_id: object
    payload: dict[str, object]
    attempt_count: int


class AssessmentDispatcher:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        starter: WorkflowStarter,
        batch_size: int,
        max_attempts: int,
        lease_seconds: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._session_factory = session_factory
        self._starter = starter
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._now = now

    async def dispatch_once(self) -> int:
        claimed = await self._claim_pending()
        published = 0
        for item in claimed:
            workflow_id = (
                f"assessment:{item.event_id}:{item.revision_id}"
            )
            try:
                await self._starter.start_assessment(
                    workflow_id=workflow_id,
                    payload={**item.payload, "outbox_id": str(item.id)},
                )
            except Exception as exc:
                await self._record_failure(item.id, item.attempt_count, exc)
            else:
                await self._record_success(item.id, item.attempt_count)
                published += 1
        return published

    async def _claim_pending(self) -> list[_ClaimedOutbox]:
        now = _normalize_utc(self._now())
        lease_until = now + timedelta(seconds=self._lease_seconds)
        async with self._session_factory() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(EventLifecycleOutbox)
                        .where(
                            EventLifecycleOutbox.trigger_type == "assessment.requested",
                            EventLifecycleOutbox.status.in_(("pending", "processing")),
                            EventLifecycleOutbox.available_at <= now,
                        )
                        .order_by(
                            EventLifecycleOutbox.available_at,
                            EventLifecycleOutbox.created_at,
                            EventLifecycleOutbox.id,
                        )
                        .limit(self._batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                claimed: list[_ClaimedOutbox] = []
                for row in rows:
                    row.status = "processing"
                    row.attempt_count += 1
                    row.available_at = lease_until
                    claimed.append(
                        _ClaimedOutbox(
                            id=row.id,
                            event_id=row.event_id,
                            revision_id=row.revision_id,
                            payload=dict(row.payload),
                            attempt_count=row.attempt_count,
                        )
                    )
                return claimed

    async def _record_success(
        self,
        outbox_id: object,
        expected_attempt_count: int,
    ) -> None:
        now = _normalize_utc(self._now())
        async with self._session_factory() as session:
            async with session.begin():
                outbox = await session.get(
                    EventLifecycleOutbox,
                    outbox_id,
                    with_for_update=True,
                )
                if not _is_current_attempt(
                    outbox,
                    expected_attempt_count=expected_attempt_count,
                ):
                    return
                outbox.status = "published"
                outbox.published_at = now
                outbox.last_error = None

    async def _record_failure(
        self,
        outbox_id: object,
        expected_attempt_count: int,
        exc: Exception,
    ) -> None:
        now = _normalize_utc(self._now())
        async with self._session_factory() as session:
            async with session.begin():
                outbox = await session.get(
                    EventLifecycleOutbox,
                    outbox_id,
                    with_for_update=True,
                )
                if not _is_current_attempt(
                    outbox,
                    expected_attempt_count=expected_attempt_count,
                ):
                    return
                outbox.last_error = _safe_error_text(exc)
                if outbox.attempt_count >= self._max_attempts:
                    outbox.status = "dead_letter"
                    outbox.available_at = now
                    return
                outbox.status = "pending"
                outbox.available_at = now + timedelta(
                    seconds=min(2 ** max(outbox.attempt_count - 1, 0), 300)
                )


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone information")
    return value.astimezone(UTC)


def _is_current_attempt(
    outbox: EventLifecycleOutbox | None,
    *,
    expected_attempt_count: int,
) -> bool:
    return (
        outbox is not None
        and outbox.status == "processing"
        and outbox.attempt_count == expected_attempt_count
    )


def _safe_error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2_000]
