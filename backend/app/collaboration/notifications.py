from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collaboration.models import NotificationDelivery
from app.collaboration.scheduler import DeadlineScheduler, SchedulerResult


@dataclass(frozen=True, slots=True)
class AdapterResult:
    sent: bool
    provider_message_id: str | None = None
    retryable: bool = True
    error: str | None = None


class NotificationAdapter(Protocol):
    async def send(self, delivery: NotificationDelivery) -> AdapterResult:
        ...


@dataclass(frozen=True, slots=True)
class DispatchResult:
    processed: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0
    fallback_sent: int = 0


class NotificationService:
    def __init__(
        self,
        adapters: dict[str, NotificationAdapter] | None = None,
        *,
        max_attempts: int = 3,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._adapters = dict(adapters or {})
        self._max_attempts = max_attempts
        self._now = now or (lambda: datetime.now(UTC))

    async def dispatch_pending(
        self,
        session: AsyncSession,
        limit: int,
    ) -> DispatchResult:
        if limit < 1:
            return DispatchResult()
        now = _as_utc(self._now())
        rows = (
            await session.scalars(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.status == "pending",
                    NotificationDelivery.available_at <= now,
                )
                .order_by(
                    NotificationDelivery.available_at,
                    NotificationDelivery.id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()

        result = DispatchResult()
        for delivery in rows:
            result = await self._process_delivery(
                session,
                delivery,
                now,
                result,
            )
        await session.flush()
        return result

    async def _process_delivery(
        self,
        session: AsyncSession,
        delivery: NotificationDelivery,
        now: datetime,
        result: DispatchResult,
    ) -> DispatchResult:
        delivery.status = "processing"
        delivery.attempt_count += 1
        delivery.updated_at = now
        delivery.last_error = None

        adapter = self._adapters.get(delivery.channel)
        adapter_result: AdapterResult
        if adapter is None:
            adapter_result = AdapterResult(
                sent=False,
                retryable=False,
                error=f"no adapter configured for {delivery.channel}",
            )
        else:
            try:
                adapter_result = await adapter.send(delivery)
            except Exception as exc:
                adapter_result = AdapterResult(
                    sent=False,
                    retryable=True,
                    error=_safe_error_text(exc),
                )

        if adapter_result.sent:
            delivery.status = "sent"
            delivery.sent_at = now
            delivery.provider_message_id = adapter_result.provider_message_id
            delivery.lease_expires_at = None
            delivery.last_error = None
            return _replace(
                result,
                processed=result.processed + 1,
                sent=result.sent + 1,
            )

        delivery.last_error = adapter_result.error
        delivery.lease_expires_at = None
        should_fallback = (
            not adapter_result.retryable
            or delivery.attempt_count >= self._max_attempts
        )
        if should_fallback:
            delivery.status = "fallback_sent"
            delivery.sent_at = now
            if delivery.channel != "in_app":
                await self._ensure_in_app_delivery(session, delivery, now)
            return _replace(
                result,
                processed=result.processed + 1,
                fallback_sent=result.fallback_sent + 1,
            )

        delivery.status = "pending"
        return _replace(
            result,
            processed=result.processed + 1,
            retried=result.retried + 1,
        )

    @staticmethod
    async def _ensure_in_app_delivery(
        session: AsyncSession,
        external: NotificationDelivery,
        now: datetime,
    ) -> None:
        existing = await session.scalar(
            select(NotificationDelivery.id).where(
                NotificationDelivery.event_id == external.event_id,
                NotificationDelivery.task_id == external.task_id,
                NotificationDelivery.recipient_user_id
                == external.recipient_user_id,
                NotificationDelivery.channel == "in_app",
                NotificationDelivery.dedupe_key == external.dedupe_key,
            )
        )
        if existing is not None:
            return
        session.add(
            NotificationDelivery(
                event_id=external.event_id,
                task_id=external.task_id,
                recipient_user_id=external.recipient_user_id,
                intent_type=external.intent_type,
                channel="in_app",
                dedupe_key=external.dedupe_key,
                status="pending",
                attempt_count=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )


def _replace(result: DispatchResult, **changes: int) -> DispatchResult:
    return DispatchResult(
        processed=changes.get("processed", result.processed),
        sent=changes.get("sent", result.sent),
        retried=changes.get("retried", result.retried),
        failed=changes.get("failed", result.failed),
        fallback_sent=changes.get("fallback_sent", result.fallback_sent),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must include timezone information")
    return value.astimezone(UTC)


def _safe_error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2_000]


__all__ = [
    "AdapterResult",
    "DeadlineScheduler",
    "DispatchResult",
    "NotificationAdapter",
    "NotificationService",
    "SchedulerResult",
]
