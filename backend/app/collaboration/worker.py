from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.command_hall.projector import CommandHallProjector
from app.collaboration.generation import CollaborationOutboxDispatcher
from app.collaboration.models import (
    CollaborationOutbox,
    CommandHallAlertProjection,
    CommandHallEventProjection,
)
from app.collaboration.notifications import (
    DispatchResult,
    NotificationService,
)
from app.collaboration.scheduler import DeadlineScheduler, SchedulerResult
from app.config import Settings, settings
from app.db import SessionFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerCycleResult:
    collaboration_dispatched: int = 0
    projection_dispatched: int = 0
    tasks_scanned: int = 0
    timeliness_updated: int = 0
    assigned_created: int = 0
    due_soon_created: int = 0
    overdue_marked: int = 0
    overdue_reminder_count: int = 0
    scheduler_ran: bool = False
    notifications_processed: int = 0
    notifications_sent: int = 0
    notifications_retried: int = 0
    notifications_failed: int = 0
    notifications_fallback_sent: int = 0

    @property
    def generated_tasks(self) -> int:
        return self.collaboration_dispatched

    def counts(self) -> dict[str, int | bool]:
        return {
            "generated_tasks": self.generated_tasks,
            "collaboration": self.collaboration_dispatched,
            "projection": self.projection_dispatched,
            "tasks_scanned": self.tasks_scanned,
            "timeliness_updated": self.timeliness_updated,
            "assigned_created": self.assigned_created,
            "due_soon_created": self.due_soon_created,
            "overdue_marked": self.overdue_marked,
            "overdue_reminder_count": self.overdue_reminder_count,
            "scheduler_ran": self.scheduler_ran,
            "notifications_processed": self.notifications_processed,
            "notifications_sent": self.notifications_sent,
            "notifications_retried": self.notifications_retried,
            "notifications_failed": self.notifications_failed,
            "notifications_fallback_sent": self.notifications_fallback_sent,
        }


class ProjectionRefresh(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        outbox: CollaborationOutbox,
    ) -> None:
        ...


@dataclass(frozen=True, slots=True)
class _ClaimedProjection:
    id: uuid.UUID
    attempt_count: int


class ProjectionOutboxDispatcher:
    def __init__(
        self,
        session_factory: object,
        *,
        handler: ProjectionRefresh | None = None,
        batch_size: int = 50,
        max_attempts: int = 10,
        lease_seconds: int = 60,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._session_factory = session_factory
        self._handler = handler
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._now = now

    async def dispatch_once(self) -> int:
        if self._handler is None:
            return 0
        await self._persist_projection_lag_alerts()
        claimed = await self._claim_pending()
        published = 0
        for item in claimed:
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        outbox = await session.get(
                            CollaborationOutbox,
                            item.id,
                            with_for_update=True,
                        )
                        if not _is_current_projection_attempt(
                            outbox,
                            expected_attempt_count=item.attempt_count,
                        ):
                            continue
                        await self._handler(session, outbox)
                        now = _as_utc(self._now())
                        outbox.status = "published"
                        outbox.dispatched_at = now
                        outbox.lease_expires_at = None
                        outbox.last_error = None
                        outbox.updated_at = now
            except Exception as exc:
                await self._record_failure(
                    item.id,
                    item.attempt_count,
                    exc,
                )
            else:
                published += 1
        return published

    async def _persist_projection_lag_alerts(self) -> None:
        now = _as_utc(self._now())
        cutoff = now - timedelta(seconds=5)
        async with self._session_factory() as session:
            async with session.begin():
                rows = (
                    await session.execute(
                        select(
                            CommandHallEventProjection.event_id,
                            CommandHallEventProjection.projection_version,
                            CollaborationOutbox.created_at,
                        )
                        .join(
                            CommandHallEventProjection,
                            CommandHallEventProjection.event_id
                            == CollaborationOutbox.event_id,
                        )
                        .where(
                            CollaborationOutbox.status.in_(
                                ("pending", "processing", "dead_letter")
                            ),
                            _projection_ready(),
                            CollaborationOutbox.created_at < cutoff,
                            CollaborationOutbox.created_at
                            > CommandHallEventProjection.updated_at,
                        )
                        .order_by(CollaborationOutbox.created_at)
                    )
                ).all()
                if not rows:
                    return

                oldest_by_event: dict[uuid.UUID, tuple[int, datetime]] = {}
                for event_id, projection_version, source_updated_at in rows:
                    oldest_by_event.setdefault(
                        event_id,
                        (projection_version, source_updated_at),
                    )

                for event_id, (
                    projection_version,
                    source_updated_at,
                ) in oldest_by_event.items():
                    lag_seconds = max(
                        0.0,
                        (now - _as_utc(source_updated_at)).total_seconds(),
                    )
                    alert = await session.scalar(
                        select(CommandHallAlertProjection)
                        .where(
                            CommandHallAlertProjection.event_id == event_id,
                            CommandHallAlertProjection.alert_key
                            == "projection.lag",
                            CommandHallAlertProjection.status == "open",
                        )
                        .with_for_update()
                    )
                    detail = {
                        "projection_lag_seconds": lag_seconds,
                        "projection_version": projection_version,
                        "source_updated_at": source_updated_at.astimezone(
                            UTC
                        ).isoformat(),
                    }
                    if alert is None:
                        session.add(
                            CommandHallAlertProjection(
                                event_id=event_id,
                                workgroup_code=None,
                                task_id=None,
                                alert_key="projection.lag",
                                alert_type="projection.lag",
                                severity="warning",
                                status="open",
                                title="数据同步中",
                                detail=detail,
                                projection_version=projection_version,
                                first_seen_at=now,
                                updated_at=now,
                            )
                        )
                    else:
                        alert.detail = detail
                        alert.projection_version = projection_version
                        alert.updated_at = now

    async def _claim_pending(self) -> list[_ClaimedProjection]:
        now = _as_utc(self._now())
        lease_until = now + timedelta(seconds=self._lease_seconds)
        async with self._session_factory() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(CollaborationOutbox)
                        .where(
                            CollaborationOutbox.status.in_(
                                ("pending", "processing")
                            ),
                            _projection_ready(),
                            CollaborationOutbox.available_at <= now,
                        )
                        .order_by(
                            CollaborationOutbox.available_at,
                            CollaborationOutbox.created_at,
                            CollaborationOutbox.id,
                        )
                        .limit(self._batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                claimed: list[_ClaimedProjection] = []
                for row in rows:
                    row.status = "processing"
                    row.attempt_count += 1
                    row.available_at = lease_until
                    row.lease_expires_at = lease_until
                    row.updated_at = now
                    claimed.append(
                        _ClaimedProjection(
                            id=row.id,
                            attempt_count=row.attempt_count,
                        )
                    )
                return claimed

    async def _record_failure(
        self,
        outbox_id: uuid.UUID,
        expected_attempt_count: int,
        exc: Exception,
    ) -> None:
        now = _as_utc(self._now())
        async with self._session_factory() as session:
            async with session.begin():
                outbox = await session.get(
                    CollaborationOutbox,
                    outbox_id,
                    with_for_update=True,
                )
                if not _is_current_projection_attempt(
                    outbox,
                    expected_attempt_count=expected_attempt_count,
                ):
                    return
                outbox.last_error = _safe_error_text(exc)
                outbox.lease_expires_at = None
                if outbox.attempt_count >= self._max_attempts:
                    outbox.status = "dead_letter"
                    outbox.available_at = now
                else:
                    outbox.status = "pending"
                    outbox.available_at = now + timedelta(
                        seconds=min(
                            2 ** max(outbox.attempt_count - 1, 0),
                            300,
                        )
                    )
                outbox.updated_at = now


async def run_worker_cycle(
    session_factory: object,
    observed_at: datetime,
    *,
    collaboration_dispatcher: object | None = None,
    projection_dispatcher: object | None = None,
    scheduler: object | None = None,
    notification_service: object | None = None,
    notification_limit: int | None = None,
    configured: Settings = settings,
) -> WorkerCycleResult:
    observed_at = _as_utc(observed_at)
    now = lambda: observed_at  # noqa: E731
    collaboration_dispatcher = (
        collaboration_dispatcher
        or CollaborationOutboxDispatcher(
            session_factory=session_factory,
            catalog_path=configured.collaboration_task_template_path,
            batch_size=configured.collaboration_worker_batch_size,
            max_attempts=configured.collaboration_outbox_max_attempts,
            lease_seconds=configured.collaboration_outbox_lease_seconds,
            now=now,
        )
    )
    projection_dispatcher = (
        projection_dispatcher
        or ProjectionOutboxDispatcher(
            session_factory,
            handler=_refresh_command_hall_projection,
            batch_size=configured.collaboration_worker_batch_size,
            max_attempts=configured.collaboration_outbox_max_attempts,
            lease_seconds=configured.collaboration_outbox_lease_seconds,
            now=now,
        )
    )
    scheduler = scheduler or DeadlineScheduler()
    notification_service = notification_service or NotificationService()

    collaboration_count = await collaboration_dispatcher.dispatch_once()
    projection_count = await projection_dispatcher.dispatch_once()

    async with session_factory() as session:
        async with session.begin():
            scheduler_result: SchedulerResult = await scheduler.run_once(
                session,
                observed_at,
            )

    limit = notification_limit or configured.collaboration_worker_batch_size
    async with session_factory() as session:
        async with session.begin():
            notification_result: DispatchResult = (
                await notification_service.dispatch_pending(session, limit)
            )

    affected_event_ids = set(
        getattr(scheduler_result, "affected_event_ids", frozenset())
    )
    affected_event_ids.update(
        getattr(notification_result, "affected_event_ids", frozenset())
    )
    if affected_event_ids:
        projector = CommandHallProjector()
        async with session_factory() as session:
            async with session.begin():
                for event_id in sorted(affected_event_ids, key=str):
                    await projector.refresh_event(session, event_id)

    result = WorkerCycleResult(
        collaboration_dispatched=collaboration_count,
        projection_dispatched=projection_count + len(affected_event_ids),
        tasks_scanned=scheduler_result.tasks_scanned,
        timeliness_updated=scheduler_result.timeliness_updated,
        assigned_created=scheduler_result.assigned_created,
        due_soon_created=scheduler_result.due_soon_created,
        overdue_marked=scheduler_result.overdue_marked,
        overdue_reminder_count=scheduler_result.overdue_reminder_count,
        scheduler_ran=True,
        notifications_processed=notification_result.processed,
        notifications_sent=notification_result.sent,
        notifications_retried=notification_result.retried,
        notifications_failed=notification_result.failed,
        notifications_fallback_sent=notification_result.fallback_sent,
    )
    _log_worker_cycle(result)
    return result


async def _refresh_command_hall_projection(
    session: AsyncSession,
    outbox: CollaborationOutbox,
) -> None:
    projector = CommandHallProjector()
    await projector.refresh_event(session, outbox.event_id)


async def run_collaboration_worker(
    stop_event: asyncio.Event | None = None,
    *,
    session_factory: object = SessionFactory,
    configured: Settings = settings,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    worker_cycle: Callable[..., Awaitable[WorkerCycleResult]] | None = None,
) -> None:
    if not configured.collaboration_worker_enabled:
        raise RuntimeError("collaboration worker is disabled")
    stop_event = stop_event or asyncio.Event()
    cycle = worker_cycle or run_worker_cycle
    while not stop_event.is_set():
        try:
            result = await cycle(
                session_factory=session_factory,
                observed_at=now(),
                configured=configured,
            )
            _log_worker_cycle(result)
        except Exception:
            logger.exception("collaboration worker cycle failed")
        await _wait_for_stop_or_sleep(
            stop_event,
            configured.collaboration_worker_poll_seconds,
            sleep,
        )


def _log_worker_cycle(result: WorkerCycleResult) -> None:
    payload = json.dumps(
        result.counts(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    logger.info("collaboration worker cycle counts=%s", payload)


async def _wait_for_stop_or_sleep(
    stop_event: asyncio.Event,
    delay: float,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    if stop_event.is_set():
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        await sleep(0)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
) -> None:
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(signum, lambda _signum, _frame: stop_event.set())


async def _run_process() -> None:
    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)
    await run_collaboration_worker(stop_event)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s",'
        '"logger":"%(name)s","message":"%(message)s"}',
    )
    try:
        asyncio.run(_run_process())
    except KeyboardInterrupt:
        pass


def _is_current_projection_attempt(
    outbox: CollaborationOutbox | None,
    *,
    expected_attempt_count: int,
) -> bool:
    return (
        outbox is not None
        and outbox.status == "processing"
        and outbox.attempt_count == expected_attempt_count
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone information")
    return value.astimezone(UTC)


def _safe_error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2_000]


def _projection_ready():
    return (
        CollaborationOutbox.payload["needs_reconcile"]
        .as_boolean()
        .is_not(True)
    )


if __name__ == "__main__":
    main()
